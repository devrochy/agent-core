"""Release del turno: asignación sticky por episodio y revalidación de tareas (ADR-0117 §4, HU-AOS-14).

Solo con `tenants.aos_runtime = 'v3'` (y `AOS_CONNECTION_ROUTING` encendida, que v3 exige): con
`legacy`, o con la bandera global apagada, `fijar_release_del_turno` no hace nada (ni una
consulta) y el runtime lee `agent_specs`, el disco y `AppSettings` como siempre.

**Asignación** (en una transacción, con la fila de la conversación bloqueada):
- Sin release, o al empezar un episodio nuevo (`episode.idleMinutes` del KernelSpec de su release,
  `core.kernel.episodio`, la única definición) → se sortea entre la activa y el canary (HU-AOS-32).
- Dentro del episodio, con la activa (o la del canary) → se queda.
- Con otra: si su release fue **retirada**
  (su último evento de ciclo de vida es `rolled_back`, `canary_stopped` o `invalid`) → la
  activa ya en este turno (el rollback llega al siguiente turno, ≤ 5 s); si no → se queda hasta
  el fin del episodio (una promoción normal no corta un episodio en curso).
- El carril es `canary` si el turno va a la candidata en prueba (HU-AOS-32): al empezar el episodio
  (o en la primera asignación) se sortea `cubeta(canary, conversación) < canary_percent`.

**Al cambiar de release** (después de expirar las tareas del episodio viejo, que lo hace
`abrir_episodio`): cada tarea abierta se revalida contra el procedimiento de la release nueva.
- Si está: evento `revalidated` con las versiones y releases de origen y destino. Conserva los
  datos que la versión nueva declara (si no declara ninguno, como un procedimiento abierto,
  todos); la etapa es la vigente si existe en la versión nueva, si no la primera (hasta el motor
  de HU-AOS-05).
- Si no está: `cancelled` con motivo `release_changed`, salvo que ya haya tenido un efecto
  irreversible: entonces `escalated` (mismo motivo). Irreversible = un `action_succeeded` de una
  acción con `side_effect = 'irreversible'` en el catálogo del binario **o sin efecto conocido**
  (hoy ninguna acción lo declara: HU-AOS-34). Escalar la conversación a una persona desde el
  orquestador es precondición de `aos_procedures` (el kernel no importa el orquestador); por eso
  se devuelven los ids escalados.

**Agente en foco y dueño** (HU-AOS-15): el turno lo atiende un especialista de la release
(`cargar.elegir_foco`): el dueño vigente (`conversations.active_agent_instance_id`) si sigue
siendo especialista de la release y el episodio no cambió, si no el `routing.defaultAgent`
del KernelSpec. Al empezar un episodio nuevo el dueño se suelta (NULL → por defecto), como
piden `kernelspec.Episode` y ADR-0116 (el handoff en dos fases es de HU-AOS-19: aquí nunca
se escribe uno, solo quién atendió). Si el que atiende cambia, se escribe
`active_agent_instance_id` y `owner_since` en la misma transacción, bajo el lock.

**Enrutamiento** (HU-AOS-17, ADR-0138): con `tenants.aos_routing` ≠ off, `core.kernel.router`
calcula la ruta (tarea en foco → reglas → disparadores → la de siempre) bajo el mismo lock. En
`shadow` solo se registra (`core_decisions.shadow_route`); en `active` su agente es el foco y una
regla `start` deja el procedimiento para el motor (`release_vigente.fijar_inicio`). Con `off`, ni
una consulta ni una lectura de Redis más.

Efecto en el turno: deja la release en `core.shared.release_vigente` (la ranura que abre
`con_release`) y en `core_decisions` (`anotar_release`).
"""

from __future__ import annotations

import functools
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.shared import release_vigente
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

if TYPE_CHECKING:
    from agentcore_contracts import Procedure

    from core.kernel.router import Ruta
    from core.kernel.state import Proyeccion
    from core.releases.cargar import ReleaseCongelada
    from core.shared.release_vigente import AgenteCongelado

log = get_logger(__name__)

MOTIVO_CAMBIO = "release_changed"
LANE_ESTABLE = "stable"
LANE_CANARY = "canary"
# = core.releases.promocion.CICLO_DE_VIDA / RETIRAN (sin importar promoción desde el kernel).
_CICLO_DE_VIDA = ("promoted", "canary_started", "rolled_back", "canary_stopped", "invalid")
_RETIRAN = frozenset({"rolled_back", "canary_stopped", "invalid"})


def con_release[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Decorador del turno: abre la ranura de la release y la cierra al terminar (también si
    falla). Va por fuera de `en_turno`, así la ranura vive mientras se escribe la decisión."""

    @functools.wraps(fn)
    async def envoltorio(*args: P.args, **kwargs: P.kwargs) -> R:
        token = release_vigente.abrir()
        try:
            return await fn(*args, **kwargs)
        finally:
            release_vigente.cerrar(token)

    return envoltorio


@dataclass(frozen=True)
class Asignacion:
    release_id: uuid.UUID
    lane: str
    motivo: str | None = None  # primera | episodio_nuevo | retirada | None (se queda)
    desde: uuid.UUID | None = None
    revalidadas: tuple[uuid.UUID, ...] = ()
    canceladas: tuple[uuid.UUID, ...] = ()
    escaladas: tuple[uuid.UUID, ...] = ()
    # Especialista que atiende el turno (HU-AOS-15) y si cambió el dueño de la conversación.
    foco: AgenteCongelado | None = None
    dueno_cambio: bool = False
    # Enrutamiento (HU-AOS-17): el modo efectivo y la ruta calculada (None con `off`). En
    # `shadow` la ruta solo se registra; en `active` su agente es el `foco`.
    modo_ruteo: str = "off"
    ruta: Ruta | None = None


@dataclass(frozen=True)
class ContextoRuteo:
    """Lo que el turno le pasa a `asignar` para enrutar (solo con `aos_routing` ≠ off)."""

    modo: str
    texto: str = ""
    payload: str = ""
    # Pila de tareas (HU-AOS-20): off | shadow | active. `active` exige además los procedimientos
    # (`aos_procedures`) y el ruteo efectivo en `active`; si no, se mide en sombra.
    pila: str = "off"
    procedimientos: bool = False

    def pila_activa(self, routing: Any) -> bool:
        from core.kernel.router import modo_efectivo

        return self.pila == "active" and self.procedimientos and modo_efectivo(self.modo, routing) == "active"


# ------------------------------------------------------------------ reglas (puras)
def cubeta(canary_id: uuid.UUID, conversation_id: uuid.UUID) -> int:
    """La cubeta 0..99 de una conversación para un canary (HU-AOS-32, adenda de ADR-0117 §4).
    sha256 y no `hash()`: la de Python cambia entre procesos. Otro canary, otro reparto."""
    digest = hashlib.sha256(f"{canary_id}|{conversation_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def decidir_destino(
    actual: uuid.UUID | None,
    activa: uuid.UUID,
    canary: uuid.UUID | None,
    *,
    episodio_nuevo: bool,
    retirada: bool,
    canary_percent: int = 0,
    conversation_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str | None]:
    """La release del turno. Solo se sortea entre la activa y el canary en la primera asignación y al
    empezar un episodio (`cubeta < canary_percent` → canary); dentro del episodio la conversación
    conserva su release, y una retirada a mitad de episodio va a la activa sin volver a sortear."""

    def sorteo() -> uuid.UUID:
        if canary is not None and canary_percent > 0 and conversation_id is not None:
            if cubeta(canary, conversation_id) < canary_percent:
                return canary
        return activa

    if actual is None:
        return sorteo(), "primera"
    if episodio_nuevo:
        destino = sorteo()
        return (destino, "episodio_nuevo") if destino != actual else (actual, None)
    if actual == activa or (canary is not None and actual == canary):
        return actual, None
    if retirada:
        return activa, "retirada"
    return actual, None


def carril(release_id: uuid.UUID, activa: uuid.UUID, canary: uuid.UUID | None) -> str:
    """`canary` si el turno va a la candidata en prueba (HU-AOS-32); si no, el estable."""
    del activa
    return LANE_CANARY if canary is not None and release_id == canary else LANE_ESTABLE


def tuvo_efecto_irreversible(eventos: Iterable[Any], catalogo: Mapping[str, Any]) -> bool:
    for evento in eventos:
        if getattr(evento, "type", None) != "action_succeeded":
            continue
        accion = (getattr(evento, "data", None) or {}).get("action")
        info = catalogo.get(accion) if isinstance(accion, str) else None
        efecto = getattr(info, "side_effect", None)
        if efecto is None or efecto == "irreversible":
            return True
    return False


def plan_revalidacion(
    tarea: Proyeccion, nuevo: Procedure, *, desde: uuid.UUID, hacia: uuid.UUID
) -> dict[str, Any]:
    """Datos del evento `revalidated` de una tarea contra la versión nueva de su procedimiento."""
    etapas = [s.id for s in nuevo.stages]
    declarados = set(nuevo.slots)
    return {
        "from_version": tarea.procedure_version,
        "to_version": nuevo.version,
        "to_stage": tarea.stage if tarea.stage in etapas else etapas[0],
        "dropped_slots": sorted(k for k in tarea.slots if declarados and k not in declarados),
        "from_release_id": str(desde),
        "to_release_id": str(hacia),
    }


# ------------------------------------------------------------------ revalidación
async def revalidar_tareas(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    desde: uuid.UUID,
    hacia: ReleaseCongelada,
    turn_id: uuid.UUID | None = None,
) -> tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]]:
    """(revalidadas, canceladas, escaladas). No hace commit: va en la transacción del cambio."""
    from core.db.models import ConversationTask
    from core.kernel import state
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    abiertas = (
        (
            await session.execute(
                select(ConversationTask)
                .where(
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_id == conversation_id,
                    ConversationTask.status.in_(state.ABIERTOS),
                )
                .order_by(ConversationTask.created_at)
            )
        )
        .scalars()
        .all()
    )
    if not abiertas:
        return [], [], []
    catalogo = catalogo_de_acciones(plugin_manager.list_modules())
    revalidadas: list[uuid.UUID] = []
    canceladas: list[uuid.UUID] = []
    escaladas: list[uuid.UUID] = []
    for fila in abiertas:
        tarea = state.proyeccion_de(fila)
        # HU-AOS-16: el mismo procedimiento con el mismo major sigue (revalidada); si desaparece o
        # cambia de major, se cancela o se escala.
        nuevo = hacia.procedimiento_para(tarea.procedure_id, tarea.procedure_version)
        if nuevo is not None:
            datos = plan_revalidacion(tarea, nuevo, desde=desde, hacia=hacia.id)
            tipo = "revalidated"
            destino = revalidadas
        else:
            eventos = await state.eventos(
                session, tenant_id=tenant_id, conversation_id=conversation_id, task_id=fila.id
            )
            irreversible = tuvo_efecto_irreversible(eventos, catalogo)
            tipo = "escalated" if irreversible else "cancelled"
            datos = {
                "reason": MOTIVO_CAMBIO,
                "from_release_id": str(desde),
                "to_release_id": str(hacia.id),
            }
            destino = escaladas if irreversible else canceladas
        await state.registrar(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            task_id=fila.id,
            tipo=tipo,
            data=datos,
            actor="kernel",
            turn_id=turn_id,
        )
        destino.append(fila.id)
    log.info(
        "tasks_release_changed",
        conversation_id=str(conversation_id),
        revalidated=len(revalidadas),
        cancelled=len(canceladas),
        escalated=len(escaladas),
    )
    return revalidadas, canceladas, escaladas


# ------------------------------------------------------------------ asignación
async def _retirada(session: AsyncSession, tenant_id: uuid.UUID, release_id: uuid.UUID) -> bool:
    ultimo = (
        await session.execute(
            text(
                "SELECT type FROM release_events WHERE tenant_id = :t AND release_id = :r "
                "AND type = ANY(:tipos) ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id, "tipos": list(_CICLO_DE_VIDA)},
        )
    ).scalar_one_or_none()
    return ultimo in _RETIRAN


async def asignar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    connection_id: uuid.UUID,
    ruteo: ContextoRuteo | None = None,
) -> tuple[Asignacion, ReleaseCongelada] | None:
    """Asigna la release del turno (ver el docstring del módulo). None si la conexión no tiene
    release activa. No hace commit. Con `ruteo`, el router elige el especialista (HU-AOS-17)."""
    from core.kernel.episodio import abrir_episodio
    from core.releases.cargar import cargar_release

    punteros = (
        await session.execute(
            text(
                "SELECT active_release_id, canary_release_id, canary_percent FROM channel_connections "
                "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL"
            ),
            {"t": tenant_id, "c": connection_id},
        )
    ).first()
    if punteros is None or punteros[0] is None:
        return None
    activa, canary, canary_percent = punteros[0], punteros[1], int(punteros[2] or 0)
    fila_conv = (
        await session.execute(
            text(
                "SELECT release_id, active_agent_instance_id, customer_id, channel, release_lane FROM conversations "
                "WHERE tenant_id = :t AND id = :c FOR UPDATE"
            ),
            {"t": tenant_id, "c": conversation_id},
        )
    ).first()
    if fila_conv is None:
        return None
    actual, dueno = fila_conv[0], fila_conv[1]
    customer_id, channel = fila_conv[2], str(fila_conv[3] or "")
    lane_actual = fila_conv[4]
    del_episodio = await cargar_release(session, tenant_id, actual or activa)
    if del_episodio is None:  # la FK compuesta lo impide; por si acaso, la activa
        del_episodio = await cargar_release(session, tenant_id, activa)
        if del_episodio is None:
            return None
    episodio = await abrir_episodio(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        idle_minutes=del_episodio.idle_minutes,
    )
    nuevo = episodio is not None and episodio.nuevo
    retirada = False
    if actual is not None and actual not in (activa, canary) and not nuevo:
        retirada = await _retirada(session, tenant_id, actual)
    destino, motivo = decidir_destino(
        actual, activa, canary, episodio_nuevo=nuevo, retirada=retirada,
        canary_percent=canary_percent, conversation_id=conversation_id,
    )
    lane = carril(destino, activa, canary)
    hacia = (
        del_episodio
        if destino == del_episodio.id
        else await cargar_release(session, tenant_id, destino)
    )
    if hacia is None:
        return None
    revalidadas: list[uuid.UUID] = []
    canceladas: list[uuid.UUID] = []
    escaladas: list[uuid.UUID] = []
    if destino != actual or lane != lane_actual:
        # HU-AOS-32: también cuando cambia solo el carril (el ex-canary vuelve a `stable` al publicarse).
        await session.execute(
            text(
                "UPDATE conversations SET release_id = :r, release_lane = :l "
                "WHERE tenant_id = :t AND id = :c"
            ),
            {"t": tenant_id, "c": conversation_id, "r": destino, "l": lane},
        )
    if destino != actual:
        if actual is not None:
            revalidadas, canceladas, escaladas = await revalidar_tareas(
                session,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                desde=actual,
                hacia=hacia,
            )
        log.info(
            "conversation_release_assigned",
            conversation_id=str(conversation_id),
            release_number=hacia.number,
            reason=motivo,
        )
    # Agente en foco (HU-AOS-15): un episodio nuevo suelta al dueño; el anterior solo cuenta si
    # sigue siendo especialista de la release de destino. Sin especialista: `ErrorDeRelease`.
    dueno_vigente = None if nuevo else dueno
    foco = hacia.foco(dueno_vigente)
    modo, ruta = "off", None
    if ruteo is not None and ruteo.pila_activa(hacia.ruteo()):
        # HU-AOS-20: sin tarea en foco y con tareas en pausa, el Coordinador retoma la primera
        # (después de completar, cancelar, escalar, un cambio de release o un cierre fuera del
        # turno). El router la lleva enseguida a su dueño (`source = task`).
        await retomar_pausada(session, tenant_id=tenant_id, conversation_id=conversation_id)
    if ruteo is not None:
        modo, ruta = await _enrutar(
            session,
            hacia,
            ruteo,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            channel=channel,
            dueno=dueno_vigente,
            legacy=foco,
        )
        if modo == "active" and ruta is not None:
            foco = ruta.agente
    dueno_cambio = foco.instance_id != dueno
    if dueno_cambio:
        await session.execute(
            text(
                "UPDATE conversations SET active_agent_instance_id = :i, owner_since = now() "
                "WHERE tenant_id = :t AND id = :c"
            ),
            {"t": tenant_id, "c": conversation_id, "i": foco.instance_id},
        )
        log.info(
            "conversation_agent_assigned",
            conversation_id=str(conversation_id),
            agent_key=foco.key,
            reason=(
                f"ruta_{ruta.source}"
                if modo == "active" and ruta is not None and ruta.source != "default"
                else "episodio_nuevo"
                if nuevo
                else ("primera" if dueno is None else "dueno_fuera_de_release")
            ),
        )
    asignacion = Asignacion(
        release_id=destino,
        lane=lane,
        motivo=motivo,
        desde=actual if destino != actual else None,
        revalidadas=tuple(revalidadas),
        canceladas=tuple(canceladas),
        escaladas=tuple(escaladas),
        foco=foco,
        dueno_cambio=dueno_cambio,
        modo_ruteo=modo,
        ruta=ruta,
    )
    return asignacion, hacia


async def _enrutar(
    session: AsyncSession,
    release: ReleaseCongelada,
    ruteo: ContextoRuteo,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    channel: str,
    dueno: uuid.UUID | None,
    legacy: AgenteCongelado,
) -> tuple[str, Ruta | None]:
    """(modo efectivo, ruta) del router (HU-AOS-17). La estabilidad del dueño solo se consulta
    cuando una regla o un disparador cambiaría al dueño vigente."""
    from core.kernel import router

    routing = release.ruteo()
    modo = router.modo_efectivo(ruteo.modo, routing)
    if modo == "off":
        return modo, None
    entrada = await router.leer_entrada(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        customer_id=customer_id,
        routing=routing,
        texto=ruteo.texto,
        payload=ruteo.payload,
        channel=channel,
        dueno=dueno,
        con_abiertas=ruteo.pila != "off",
    )
    ruta = router.decidir(release, routing, entrada, legacy)
    if ruta.source in ("rule", "trigger") and dueno is not None and ruta.agente.instance_id != dueno:
        cambios = await router.cambios_de_dueno(session, tenant_id, conversation_id)
        ruta = router.estabilizar(ruta, entrada, legacy, routing, cambios)
    if ruteo.pila != "off":
        ruta = await _pila(
            session, release, routing, entrada, ruta, legacy,
            tenant_id=tenant_id, conversation_id=conversation_id, activa=ruteo.pila_activa(routing),
        )
    log.info(
        "kernel_route",
        mode=modo,
        source=ruta.source,
        agent_key=ruta.agente.key,
        rule_id=ruta.rule_id,
        procedure_id=ruta.procedure_id,
        candidates=len(ruta.candidates),
    )
    return modo, ruta


async def retomar_pausada(session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> uuid.UUID | None:
    """Sin tarea en foco, la primera en pausa (por posición y antigüedad) vuelve al foco (`resumed`,
    actor `kernel`). Las posiciones no se renumeran: todo ordena por `(stack_position, created_at)`.
    Corre con la conversación bloqueada por `asignar`. Devuelve la tarea retomada, si hubo."""
    from core.kernel import state

    filas = await state.tareas_abiertas(session, tenant_id=tenant_id, conversation_id=conversation_id)
    if any(f.status == "active" and f.stack_position == 0 for f in filas):
        return None
    pausada = next((f for f in filas if f.status == "suspended"), None)
    if pausada is None:
        return None
    from core.shared import turno as turno_vigente

    await state.registrar(
        session, tenant_id=tenant_id, conversation_id=conversation_id, task_id=pausada.id, tipo="resumed",
        data={"reason": "pila"}, actor="kernel", turn_id=turno_vigente.turno_id(),
    )
    log.info("kernel_pila_retomada", procedure_id=pausada.procedure_id)
    return pausada.id


async def _pila(
    session: AsyncSession,
    release: ReleaseCongelada,
    routing: Any,
    entrada: Any,
    ruta: Ruta,
    legacy: AgenteCongelado,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    activa: bool,
) -> Ruta:
    """La pila de tareas en la ruta (adenda HU-AOS-20 de ADR-0116). En sombra solo anota el plan
    (`pila_sombra`). Activa:

    - **sin foco:** atiende el dueño de la primera intención, que se abre en foco (`start`), y las
      demás van en pausa con su dueño. Si la estabilidad del dueño frena ese cambio, no hay pila
      en el turno. Una regla `start` va primero y las intenciones detrás; una regla que elige
      agente (sin `start`) manda y no hay pila;
    - **con foco** (digresión): todas en pausa; el agente no cambia.

    Lo que se abre y lo que sobra lo recalcula el motor bajo el lock. Nunca rompe el turno."""
    import dataclasses

    from core.kernel import router

    try:
        intenciones = router.detectar_intenciones(release, routing, entrada)
        max_stack = release.procedimientos_kernel().maxStack
        plan = router.plan_de_pila(
            intenciones, con_foco=entrada.tarea is not None, abiertas=len(entrada.abiertas), max_stack=max_stack
        )
    except Exception as exc:
        log.warning("kernel_pila_error", error=type(exc).__name__)
        return ruta
    if plan is None:
        return ruta
    log.info(
        "kernel_pila", activa=activa, abre=len(plan["abre"]), pausa=len(plan["pausa"]), sobrantes=len(plan["sobrantes"])
    )
    if not activa:
        return dataclasses.replace(ruta, pila_sombra=plan)

    def con_dueno(pids: Iterable[str]) -> tuple[tuple[str, uuid.UUID | None], ...]:
        salida: list[tuple[str, uuid.UUID | None]] = []
        for pid in pids:
            agente = router.dueno_de(release, routing, pid, entrada.dueno)
            salida.append((pid, agente.instance_id if agente is not None else None))
        return tuple(salida)

    if entrada.tarea is not None:
        return dataclasses.replace(ruta, pila=con_dueno(intenciones), max_stack=max_stack)
    if ruta.source == "rule":
        if ruta.start is None:
            return ruta
        return dataclasses.replace(ruta, pila=con_dueno(p for p in intenciones if p != ruta.start), max_stack=max_stack)
    if not plan["abre"]:  # la pila ya está llena: el motor lo anota todo como sobrante
        return dataclasses.replace(ruta, pila=con_dueno(intenciones), max_stack=max_stack)
    primero = intenciones[0]
    agente = router.dueno_de(release, routing, primero, entrada.dueno)
    if agente is None:
        return ruta
    candidata = router.Ruta(
        agente=agente,
        source="trigger",
        procedure_id=primero,
        start=primero,
        confidence=next((c["score"] for c in ruta.candidates if c["procedure_id"] == primero), None),
        candidates=ruta.candidates,
        pila=con_dueno(intenciones[1:]),
        max_stack=max_stack,
    )
    if entrada.dueno is not None and agente.instance_id != entrada.dueno:
        cambios = await router.cambios_de_dueno(session, tenant_id, conversation_id)
        if router.estabilizar(candidata, entrada, legacy, routing, cambios).frenada_por_estabilidad:
            log.info("kernel_pila_frenada_por_estabilidad")
            return ruta
    return candidata


async def _es_conexion_de_pruebas(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
    from core.db.session import get_session
    from core.gateway.connection_types import PLAYGROUND

    async with get_session() as session:
        tipo = (
            await session.execute(
                text("SELECT type_id FROM channel_connections WHERE tenant_id = :t AND id = :c"),
                {"t": tenant_id, "c": connection_id},
            )
        ).scalar_one_or_none()
    return tipo == PLAYGROUND


async def fijar_release_del_turno(conversation: Any, texto: str = "", *, enrutar: bool = True) -> Asignacion | None:
    """Punto de enganche del loop. None (sin consultas) con `AOS_CONNECTION_ROUTING` apagada;
    None con `aos_runtime = 'legacy'`, sin conexión o sin release activa (el turno sigue como
    siempre). Fail-closed con v3: un error de base sube por el camino de error del turno.
    `texto`: el mensaje del cliente, para el router (solo con `aos_routing` ≠ off). `enrutar=False`:
    asigna la release y el dueño sticky SIN correr el router ni consumir el payload de botón que
    espera el turno diferido (lo usa el toque de una opción directa, que no es un turno)."""
    if not get_settings().aos_connection_routing:
        return None
    tenant_id = get_current_tenant_id()
    connection_id = getattr(conversation, "connection_id", None)
    if (
        tenant_id is None
        or connection_id is None
        or str(getattr(conversation, "tenant_id", "")) != str(tenant_id)
    ):
        return None
    from core.db.session import get_session
    from core.kernel.banderas import banderas_del_negocio
    from core.kernel.decisions import anotar_release, anotar_ruta

    async with get_session() as session:
        banderas = await banderas_del_negocio(session, tenant_id)
        if banderas.runtime != "v3":
            return None
        ruteo = None
        if enrutar and banderas.ruteo != "off":
            from core.kernel.router import tomar_payload

            payload = await tomar_payload(tenant_id, conversation.id)
            ruteo = ContextoRuteo(
                modo=banderas.ruteo,
                texto=texto or "",
                payload=payload,
                pila=banderas.colaboracion.pila,
                procedimientos=banderas.procedimientos,
            )
        resultado = await asignar(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            connection_id=connection_id,
            ruteo=ruteo,
        )
    if resultado is None and await _es_conexion_de_pruebas(tenant_id, connection_id):
        # El Centro de pruebas va por su conexión interna, que no tiene release (ni la necesita): el
        # turno sigue por el camino de siempre y no es un `release_missing`. (Un journey de evals
        # sobre una conexión real, aunque sea del canal de prueba, sí exige su release.)
        return None
    if resultado is None:
        # v3 sin release activa: el turno sigue con el v2 (legacy silencioso). Pendiente con
        # dueño (adenda HU-AOS-15 de ADR-0117): release al alta de la conexión.
        log.error("release_missing", connection_id=str(connection_id))
        release_vigente.marcar_v3_sin_release(tenant_id)
        return None
    asignacion, release = resultado
    release_vigente.fijar(release.artefactos(asignacion.lane, foco=asignacion.foco))
    anotar_release(
        asignacion.release_id,
        asignacion.lane,
        active_instance_id=asignacion.foco.instance_id if asignacion.foco is not None else None,
    )
    if asignacion.ruta is not None:
        anotar_ruta(asignacion.ruta, activa=asignacion.modo_ruteo == "active")
        ruta = asignacion.ruta
        if asignacion.modo_ruteo == "active" and (ruta.start is not None or ruta.pila):
            release_vigente.fijar_inicio(tenant_id, ruta.start, pila=ruta.pila, max_stack=ruta.max_stack)
    return asignacion


__all__ = [
    "LANE_CANARY",
    "LANE_ESTABLE",
    "MOTIVO_CAMBIO",
    "Asignacion",
    "ContextoRuteo",
    "asignar",
    "carril",
    "con_release",
    "cubeta",
    "decidir_destino",
    "fijar_release_del_turno",
    "plan_revalidacion",
    "retomar_pausada",
    "revalidar_tareas",
    "tuvo_efecto_irreversible",
]
