"""Release del turno: asignación sticky por episodio y revalidación de tareas (ADR-0117 §4, HU-AOS-14).

Solo con `tenants.aos_runtime = 'v3'` (y `AOS_CONNECTION_ROUTING` encendida, que v3 exige): con
`legacy`, o con la bandera global apagada, `fijar_release_del_turno` no hace nada (ni una
consulta) y el runtime lee `agent_specs`, el disco y `AppSettings` como siempre.

**Asignación** (en una transacción, con la fila de la conversación bloqueada):
- Sin release → la activa de la conexión.
- Con la activa (o la del canary) → se queda.
- Con otra: si empieza un episodio nuevo (`episode.idleMinutes` del KernelSpec de su release,
  `core.kernel.episodio`, la única definición) → la activa; si su release fue **retirada**
  (su último evento de ciclo de vida es `rolled_back`, `canary_stopped` o `invalid`) → la
  activa ya en este turno (el rollback llega al siguiente turno, ≤ 5 s); si no → se queda hasta
  el fin del episodio (una promoción normal no corta un episodio en curso).
- El carril es `stable`: el reparto del canary (`carril`) es el punto de enganche de HU-AOS-32.

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

Efecto en el turno: deja la release en `core.shared.release_vigente` (la ranura que abre
`con_release`) y en `core_decisions` (`anotar_release`).
"""

from __future__ import annotations

import functools
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

    from core.kernel.state import Proyeccion
    from core.releases.cargar import ReleaseCongelada

log = get_logger(__name__)

MOTIVO_CAMBIO = "release_changed"
LANE_ESTABLE = "stable"
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


# ------------------------------------------------------------------ reglas (puras)
def decidir_destino(
    actual: uuid.UUID | None,
    activa: uuid.UUID,
    canary: uuid.UUID | None,
    *,
    episodio_nuevo: bool,
    retirada: bool,
) -> tuple[uuid.UUID, str | None]:
    if actual is None:
        return activa, "primera"
    if actual == activa or (canary is not None and actual == canary):
        return actual, None
    if episodio_nuevo:
        return activa, "episodio_nuevo"
    if retirada:
        return activa, "retirada"
    return actual, None


def carril(release_id: uuid.UUID, activa: uuid.UUID, canary: uuid.UUID | None) -> str:
    """Punto de enganche del canary (HU-AOS-32: `hash(canary_release_id ‖ conversación) % 100`
    al empezar el episodio). Hoy toda conversación va por el carril estable."""
    del release_id, activa, canary
    return LANE_ESTABLE


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
    procedimientos = hacia.procedimientos()
    catalogo = catalogo_de_acciones(plugin_manager.list_modules())
    revalidadas: list[uuid.UUID] = []
    canceladas: list[uuid.UUID] = []
    escaladas: list[uuid.UUID] = []
    for fila in abiertas:
        tarea = state.proyeccion_de(fila)
        nuevo = procedimientos.get(tarea.procedure_id)
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
) -> tuple[Asignacion, ReleaseCongelada] | None:
    """Asigna la release del turno (ver el docstring del módulo). None si la conexión no tiene
    release activa. No hace commit."""
    from core.kernel.episodio import abrir_episodio
    from core.releases.cargar import cargar_release

    punteros = (
        await session.execute(
            text(
                "SELECT active_release_id, canary_release_id FROM channel_connections "
                "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL"
            ),
            {"t": tenant_id, "c": connection_id},
        )
    ).first()
    if punteros is None or punteros[0] is None:
        return None
    activa, canary = punteros
    actual = (
        await session.execute(
            text(
                "SELECT release_id FROM conversations WHERE tenant_id = :t AND id = :c FOR UPDATE"
            ),
            {"t": tenant_id, "c": conversation_id},
        )
    ).scalar_one_or_none()
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
        actual, activa, canary, episodio_nuevo=nuevo, retirada=retirada
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
    if destino != actual:
        await session.execute(
            text(
                "UPDATE conversations SET release_id = :r, release_lane = :l "
                "WHERE tenant_id = :t AND id = :c"
            ),
            {"t": tenant_id, "c": conversation_id, "r": destino, "l": lane},
        )
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
    asignacion = Asignacion(
        release_id=destino,
        lane=lane,
        motivo=motivo,
        desde=actual if destino != actual else None,
        revalidadas=tuple(revalidadas),
        canceladas=tuple(canceladas),
        escaladas=tuple(escaladas),
    )
    return asignacion, hacia


async def fijar_release_del_turno(conversation: Any) -> Asignacion | None:
    """Punto de enganche del loop. None (sin consultas) con `AOS_CONNECTION_ROUTING` apagada;
    None con `aos_runtime = 'legacy'`, sin conexión o sin release activa (el turno sigue como
    siempre). Fail-closed con v3: un error de base sube por el camino de error del turno."""
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
    from core.kernel.decisions import anotar_release

    async with get_session() as session:
        if (await banderas_del_negocio(session, tenant_id)).runtime != "v3":
            return None
        resultado = await asignar(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            connection_id=connection_id,
        )
    if resultado is None:
        log.warning("release_missing", connection_id=str(connection_id))
        return None
    asignacion, release = resultado
    release_vigente.fijar(release.artefactos(asignacion.lane))
    anotar_release(asignacion.release_id, asignacion.lane)
    return asignacion


__all__ = [
    "LANE_ESTABLE",
    "MOTIVO_CAMBIO",
    "Asignacion",
    "asignar",
    "carril",
    "con_release",
    "decidir_destino",
    "fijar_release_del_turno",
    "plan_revalidacion",
    "revalidar_tareas",
    "tuvo_efecto_irreversible",
]
