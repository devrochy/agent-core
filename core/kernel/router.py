"""Router del Coordinador: qué especialista atiende el turno (HU-AOS-17, ADR-0138; D-AOS-8).

Solo con `tenants.aos_routing` distinto de `off` (exige `aos_runtime = 'v3'`); lo llama
`release_del_turno.asignar` con la conversación bloqueada. Sin LLM y sin embeddings. En orden:

1. **Tarea en foco** (`conversation_tasks` activa en la posición 0): atiende su dueño
   (`owner_instance_id`, o el especialista que declara su procedimiento), sin puntuar el texto
   (`task`). Una regla con `when.flowStage` que coincide con la etapa en foco sí aplica antes:
   es una regla sobre la tarea.
2. **Reglas** del KernelSpec (`routing.rules`, DSL sin regex), la primera que cumple todas sus
   condiciones (`rule`): `agent` → ese especialista; `start` → el especialista que declara el
   procedimiento, y el motor lo inicia al empezar el turno (`ContextoMotor.al_empezar_turno`,
   con sus guardas de salud y de tarea en curso; nunca aquí).
3. **Disparadores** (`core.kernel.disparadores`, léxico): con un candidato fuerte (≥
   `strongScore` y con `margin` sobre el segundo) atiende su dueño (`trigger`). No inicia nada:
   el hilo elige con `iniciar_procedimiento` (elección restringida de siempre).
4. **Por defecto**: el foco de siempre (`elegir_foco`: dueño vigente → `is_default` →
   `defaultAgent` → único) (`default`).

El coprocesador LLM (`routing.coprocessor`) no se ejecuta: con `whenCandidatesAbove` (8) por
encima de `maxCandidates` (6) nunca aplicaría, y exige un LLM (pendiente, ADR-0138).

**Estabilidad** (`routing.owner.maxOwnerChangesPer6Turns`): una regla o un disparador que cambiaría
al dueño vigente no lo cambia si en los últimos 6 turnos ya hubo ese número de cambios; queda
`default` y se anota. Cambiar quién atiende NO es un handoff (HU-AOS-19): el dueño se escribe en
`conversations.active_agent_instance_id` como en HU-AOS-15.

Este módulo no escribe nada: devuelve la `Ruta` y sus lecturas (`leer_entrada`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.shared.logger import get_logger

if TYPE_CHECKING:
    from agentcore_contracts.kernelspec import Routing, RoutingRule

    from core.releases.cargar import ReleaseCongelada
    from core.shared.release_vigente import AgenteCongelado

log = get_logger(__name__)

MODOS = ("off", "shadow", "active")
# Tope del texto que miran las reglas (`keywordAny`); el puntaje recorta aún más.
MAX_TEXTO = 2000
FUENTES = ("task", "rule", "trigger", "coprocessor", "default")
VENTANA_ESTABILIDAD = 6


def modo_efectivo(bandera: str, routing: Routing | None) -> str:
    """La bandera del negocio, salvo que el KernelSpec congelado pida sombra (solo baja)."""
    modo = bandera if bandera in MODOS else "off"
    if modo == "active" and routing is not None and routing.shadowMode:
        return "shadow"
    return modo


@dataclass(frozen=True)
class TareaEnFoco:
    task_id: uuid.UUID
    procedure_id: str
    stage: str
    owner_instance_id: uuid.UUID | None


@dataclass(frozen=True)
class Entrada:
    """Lo que el router sabe del turno. `hechos` y `hora_local` solo se leen si alguna regla los
    usa (`leer_entrada`)."""

    texto: str = ""
    payload: str = ""
    channel: str = ""
    dueno: uuid.UUID | None = None
    tarea: TareaEnFoco | None = None
    hechos: Mapping[tuple[str, str], Any] = field(default_factory=dict)
    hora_local: str | None = None


@dataclass(frozen=True)
class Ruta:
    agente: AgenteCongelado
    source: str
    rule_id: str | None = None
    # El procedimiento que motivó la ruta: el de la tarea, el de la regla o el del disparador.
    procedure_id: str | None = None
    # Solo una regla `start`: el procedimiento que el motor inicia al empezar el turno.
    start: str | None = None
    confidence: float | None = None
    candidates: tuple[dict[str, Any], ...] = ()
    focus_task_id: uuid.UUID | None = None
    # Una regla o disparador que no cambió de dueño por la estabilidad (queda `default`).
    frenada_por_estabilidad: bool = False

    def sombra(self) -> dict[str, Any]:
        """Lo que se guarda en `core_decisions.shadow_route`: la decisión, sin texto del cliente."""
        return {
            "agent_key": self.agente.key,
            "instance_id": str(self.agente.instance_id),
            "route_source": self.source,
            "rule_id": self.rule_id,
            "procedure_id": self.procedure_id,
            "start": self.start,
            "confidence": self.confidence,
        }


# ------------------------------------------------------------------ reglas (puras)
def _en_franja(hora: str | None, desde: str, hasta: str) -> bool:
    if hora is None:
        return False
    if desde <= hasta:
        return desde <= hora < hasta
    return hora >= desde or hora < hasta  # cruza la medianoche


def _igual(valor: Any, esperado: Any) -> bool:
    if isinstance(esperado, bool) or isinstance(valor, bool):
        return valor is esperado
    if isinstance(esperado, int | float) and isinstance(valor, int | float):
        return float(valor) == float(esperado)
    return isinstance(valor, str) and isinstance(esperado, str) and valor.strip().lower() == esperado.strip().lower()


def cumple(regla: RoutingRule, entrada: Entrada) -> bool:
    """Todas las condiciones presentes (AND)."""
    from core.kernel.procedures.textos import normalizar

    w = regla.when
    if w.interactivePayloadPrefix is not None and not entrada.payload.startswith(w.interactivePayloadPrefix):
        return False
    if w.keywordAny is not None:
        texto = f" {normalizar(entrada.texto)} "
        if not any((n := normalizar(str(f))) and f" {n} " in texto for f in w.keywordAny):
            return False
    if w.customerFact is not None:
        clave = (w.customerFact.namespace, w.customerFact.key)
        if clave not in entrada.hechos or not _igual(entrada.hechos[clave], w.customerFact.equals):
            return False
    if w.flowStage is not None and (
        entrada.tarea is None
        or entrada.tarea.procedure_id != w.flowStage.procedure
        or entrada.tarea.stage != w.flowStage.stage
    ):
        return False
    if w.channel is not None and entrada.channel != w.channel:
        return False
    return not (w.withinHours is not None and not _en_franja(entrada.hora_local, w.withinHours.from_, w.withinHours.to))


# ------------------------------------------------------------------ decisión (pura)
class _Release:
    """Lo que el router lee de la release (inmutable, en caché con ella)."""

    def __init__(self, release: ReleaseCongelada) -> None:
        self.release = release
        self.especialistas = [a for a in release.componentes if a.role == "specialist"]
        self.por_key = {a.key: a for a in self.especialistas}
        self.por_instancia = {a.instance_id: a for a in self.especialistas}

    def duenos_de(self, procedure_id: str) -> list[AgenteCongelado]:
        return [
            a
            for a in self.especialistas
            if a.procedimientos is not None and any(pid == procedure_id for pid, _v in a.procedimientos)
        ]


def _elegir_dueno(candidatos: Sequence[AgenteCongelado], dueno: uuid.UUID | None, legacy: AgenteCongelado) -> AgenteCongelado | None:
    """Entre varios especialistas que declaran el mismo procedimiento: el dueño vigente, luego el
    de siempre, luego el primero por key."""
    if not candidatos:
        return None
    for preferido in (dueno, legacy.instance_id):
        elegido = next((a for a in candidatos if a.instance_id == preferido), None)
        if elegido is not None:
            return elegido
    return sorted(candidatos, key=lambda a: a.key)[0]


def decidir(
    release: ReleaseCongelada,
    routing: Routing,
    entrada: Entrada,
    legacy: AgenteCongelado,
    *,
    cambios_recientes: int | None = None,
) -> Ruta:
    """La ruta del turno. `legacy` es el foco de siempre (`elegir_foco`). `cambios_recientes`: los
    cambios de dueño en los últimos turnos (None = no se sabe, sin freno)."""
    r = _Release(release)
    candidatos = _candidatos(release, routing, entrada) if entrada.tarea is None else ()

    # 1. Tarea en foco (con la excepción de las reglas sobre su etapa).
    if entrada.tarea is not None:
        for regla in routing.rules:
            if regla.when.flowStage is not None and cumple(regla, entrada):
                ruta = _por_regla(r, regla, entrada, legacy, candidatos)
                if ruta is not None:
                    return estabilizar(ruta, entrada, legacy, routing, cambios_recientes)
        tarea = entrada.tarea
        dueno = r.por_instancia.get(tarea.owner_instance_id) if tarea.owner_instance_id else None
        dueno = dueno or _elegir_dueno(r.duenos_de(tarea.procedure_id), entrada.dueno, legacy)
        return Ruta(
            agente=dueno or legacy,
            source="task",
            procedure_id=tarea.procedure_id,
            focus_task_id=tarea.task_id,
        )

    # 2. Reglas.
    for regla in routing.rules:
        if regla.when.flowStage is None and cumple(regla, entrada):
            ruta = _por_regla(r, regla, entrada, legacy, candidatos)
            if ruta is not None:
                return estabilizar(ruta, entrada, legacy, routing, cambios_recientes)

    # 3. Disparadores.
    from core.kernel.disparadores import Puntaje, fuerte

    puntajes = [Puntaje(c["procedure_id"], c["score"]) for c in candidatos]
    elegido = fuerte(puntajes, strong=routing.triggers.strongScore, margin=routing.triggers.margin)
    if elegido is not None:
        agente = _elegir_dueno(r.duenos_de(elegido.procedure_id), entrada.dueno, legacy)
        if agente is not None:
            ruta = Ruta(
                agente=agente,
                source="trigger",
                procedure_id=elegido.procedure_id,
                confidence=elegido.score,
                candidates=candidatos,
            )
            return estabilizar(ruta, entrada, legacy, routing, cambios_recientes)

    # 4. Por defecto.
    return Ruta(agente=legacy, source="default", candidates=candidatos)


def _por_regla(
    r: _Release, regla: RoutingRule, entrada: Entrada, legacy: AgenteCongelado, candidatos: tuple[dict[str, Any], ...]
) -> Ruta | None:
    if regla.agent is not None:
        agente = r.por_key.get(regla.agent)
        if agente is None:  # el build lo rechaza; una release vieja no rompe el turno
            log.warning("ruteo_regla_sin_agente", rule_id=regla.id)
            return None
        return Ruta(agente=agente, source="rule", rule_id=regla.id, confidence=1.0, candidates=candidatos)
    assert regla.start is not None
    duenos = r.duenos_de(regla.start)
    if duenos:
        agente = _elegir_dueno(duenos, entrada.dueno, legacy)
    elif not r.release.con_procedimientos_por_agente:
        agente = legacy  # `release/1`: sin procedimientos por agente, atiende el de siempre
    else:
        log.warning("ruteo_regla_sin_dueno", rule_id=regla.id)
        return None
    assert agente is not None
    # Con una tarea en foco el motor no inicia otra (`tarea_en_curso`): no se pide.
    start = regla.start if entrada.tarea is None else None
    return Ruta(
        agente=agente,
        source="rule",
        rule_id=regla.id,
        procedure_id=regla.start,
        start=start,
        confidence=1.0,
        candidates=candidatos,
        focus_task_id=entrada.tarea.task_id if entrada.tarea is not None else None,
    )


def estabilizar(
    ruta: Ruta, entrada: Entrada, legacy: AgenteCongelado, routing: Routing, cambios_recientes: int | None
) -> Ruta:
    cambia = entrada.dueno is not None and ruta.agente.instance_id != entrada.dueno
    if not cambia or cambios_recientes is None or cambios_recientes < routing.owner.maxOwnerChangesPer6Turns:
        return ruta
    log.info("ruteo_frenado_por_estabilidad", source=ruta.source, rule_id=ruta.rule_id)
    return Ruta(
        agente=legacy,
        source="default",
        candidates=ruta.candidates,
        focus_task_id=ruta.focus_task_id,
        frenada_por_estabilidad=True,
    )


def _candidatos(release: ReleaseCongelada, routing: Routing, entrada: Entrada) -> tuple[dict[str, Any], ...]:
    """Procedimientos ofrecidos con su puntaje (≥ `minScore`, los `maxCandidates` mejores):
    `core_decisions.candidates`."""
    if not entrada.texto.strip():
        return ()
    indice, duenos = release.indice_de_disparadores()
    salida: list[dict[str, Any]] = []
    for p in indice.puntuar(entrada.texto):
        if p.score < routing.triggers.minScore or len(salida) >= routing.triggers.maxCandidates:
            break
        salida.append({"procedure_id": p.procedure_id, "owner": duenos.get(p.procedure_id), "score": p.score})
    return tuple(salida)


def necesita(routing: Routing) -> tuple[set[tuple[str, str]], bool]:
    """Los hechos del cliente y si la hora local hacen falta para evaluar las reglas."""
    hechos = {
        (r.when.customerFact.namespace, r.when.customerFact.key) for r in routing.rules if r.when.customerFact is not None
    }
    return hechos, any(r.when.withinHours is not None for r in routing.rules)


# ------------------------------------------------------------------ lecturas (en la sesión de `asignar`)
async def tarea_en_foco(session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> TareaEnFoco | None:
    fila = (
        await session.execute(
            text(
                "SELECT id, procedure_id, stage, owner_instance_id FROM conversation_tasks "
                "WHERE tenant_id = :t AND conversation_id = :c AND status = 'active' "
                "AND stack_position = 0 AND parent_task_id IS NULL LIMIT 1"
            ),
            {"t": tenant_id, "c": conversation_id},
        )
    ).first()
    if fila is None:
        return None
    return TareaEnFoco(task_id=fila[0], procedure_id=str(fila[1]), stage=str(fila[2]), owner_instance_id=fila[3])


async def _hechos(
    session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID, claves: set[tuple[str, str]]
) -> dict[tuple[str, str], Any]:
    if not claves:
        return {}
    filas = await session.execute(
        text(
            "SELECT namespace, key, value FROM memory_facts WHERE tenant_id = :t AND customer_id = :c "
            "AND superseded_at IS NULL AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at > now()) "
            # Solo hechos del sistema (acciones, personas, importación): uno que el modelo extrajo
            # de lo que dijo el cliente no decide la ruta (invariante 7).
            "AND source <> 'agent' "
            "AND namespace = ANY(:ns) AND key = ANY(:ks)"
        ),
        {"t": tenant_id, "c": customer_id, "ns": sorted({n for n, _ in claves}), "ks": sorted({k for _, k in claves})},
    )
    return {(str(f[0]), str(f[1])): f[2] for f in filas if (str(f[0]), str(f[1])) in claves}


async def _hora_local(session: AsyncSession, tenant_id: uuid.UUID, ahora: datetime | None = None) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    zona = (
        await session.execute(
            text("SELECT timezone FROM app_settings WHERE tenant_id = :t AND deleted_at IS NULL LIMIT 1"),
            {"t": tenant_id},
        )
    ).scalar_one_or_none()
    try:
        tz = ZoneInfo(str(zona or "America/Bogota"))
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("ruteo_zona_invalida")
        tz = ZoneInfo("UTC")
    return (ahora or datetime.now(UTC)).astimezone(tz).strftime("%H:%M")


async def leer_entrada(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    routing: Routing,
    texto: str,
    payload: str,
    channel: str,
    dueno: uuid.UUID | None,
) -> Entrada:
    """La entrada del router: la tarea en foco siempre; hechos del cliente y hora local solo si
    alguna regla los usa."""
    claves, con_hora = necesita(routing)
    return Entrada(
        texto=(texto or "")[:MAX_TEXTO],
        payload=payload,
        channel=channel,
        dueno=dueno,
        tarea=await tarea_en_foco(session, tenant_id, conversation_id),
        hechos=await _hechos(session, tenant_id, customer_id, claves) if customer_id is not None else {},
        hora_local=await _hora_local(session, tenant_id) if con_hora else None,
    )


async def cambios_de_dueno(session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> int:
    """Cambios de quién atendió en los últimos turnos enrutados (best-effort: el ledger lo es,
    así que puede contar de menos)."""
    filas = (
        await session.execute(
            text(
                "SELECT active_instance_id FROM core_decisions WHERE tenant_id = :t AND conversation_id = :c "
                "AND outcome = 'routed' AND active_instance_id IS NOT NULL ORDER BY created_at DESC LIMIT :n"
            ),
            {"t": tenant_id, "c": conversation_id, "n": VENTANA_ESTABILIDAD},
        )
    ).all()
    valores = [f[0] for f in filas]
    return sum(1 for a, b in pairwise(valores) if a != b)


# ------------------------------------------------------------------ payload del botón (Redis)
# `aos:ruteo:payload:{negocio}:{conversación}` (convención de MULTITENANT: el negocio primero). Lo
# escribe `entrada_sin_llm` (consumer) solo con `aos_routing` ≠ off y lo consume el turno con
# GETDEL: con debounce el turno corre en otra tarea y el payload no viaja en `latest_message`.
_PAYLOAD = "aos:ruteo:payload"
# Corto: un payload de un turno que se abortó no debe disparar una regla en un mensaje posterior.
_TTL_PAYLOAD_S = 120
MAX_PAYLOAD = 256


def _clave_payload(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    return f"{_PAYLOAD}:{tenant_id}:{conversation_id}"


async def guardar_payload(tenant_id: uuid.UUID, conversation_id: uuid.UUID, payload: str) -> None:
    """Best-effort: sin el payload, una regla de botón no aplica y atiende el de siempre. Solo
    dentro del `tenant_scope` del mismo negocio."""
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        log.warning("ruteo_payload_fuera_de_scope")
        return
    try:
        from core.memory.working import get_redis

        await get_redis().set(_clave_payload(tenant_id, conversation_id), payload[:MAX_PAYLOAD], ex=_TTL_PAYLOAD_S)
    except Exception as exc:
        log.warning("ruteo_payload_guardar_fallo", error=type(exc).__name__)


async def tomar_payload(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    try:
        from core.memory.working import get_redis

        valor = await get_redis().getdel(_clave_payload(tenant_id, conversation_id))
    except Exception as exc:
        log.warning("ruteo_payload_lectura_fallo", error=type(exc).__name__)
        return ""
    if isinstance(valor, bytes):
        valor = valor.decode("utf-8", "replace")
    return str(valor or "")[:MAX_PAYLOAD]


__all__ = [
    "FUENTES",
    "MODOS",
    "Entrada",
    "Ruta",
    "TareaEnFoco",
    "cambios_de_dueno",
    "cumple",
    "decidir",
    "estabilizar",
    "guardar_payload",
    "leer_entrada",
    "modo_efectivo",
    "necesita",
    "tarea_en_foco",
    "tomar_payload",
]
