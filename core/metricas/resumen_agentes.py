"""Resumen por agente del negocio (ADR-0171). Solo lectura.

Un solo recorrido para el «Resumen» del panel y de la app:

- **General** (`general`): KPIs del negocio entero, que es lo que gobierna el Coordinador;
- **Agentes** (`agentes`): por cada agente (sin el Coordinador) su actividad, y bajo él los
  **módulos** que gestiona (`modulos`), calculados por el pack (`resumen_registry`).

Cada KPI trae el valor del período (7 d) y el de los 7 d previos para el delta; las fotos de hoy
(escalaciones pendientes, tareas abiertas) van sin previo. Los enlaces son semánticos (`Destino`).

Atribución (igual que «Mi agente y su data», para que la cifra del Resumen coincida con la del
detalle al que enlaza): turnos, conversaciones y costo salen de `usage_ledger.agent_instance_id`, y
**el General absorbe lo no atribuido**. Los escalados por agente salen de `turn_outcomes` y solo
existen con runtime v3. El módulo de un agente es el del pack cuya `agent_key` es la clave de su
plantilla; los módulos que ningún agente cubre cuelgan del General.

El Coordinador (`kind=core`) no sale como agente: sus turnos cuentan en el bloque del negocio y no en
ninguna tarjeta, así que la suma por agente puede quedar por debajo del total.

Permisos: cada KPI se omite (no sale en cero) si el admin no tiene la sección que lo respalda. Las
tasas salen `None` bajo la muestra mínima de fiabilidad. Todo filtra `tenant_id` además de RLS y
exige el `tenant_scope` del negocio. Un módulo que falla se informa en su `error` y no tumba el resto.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, text

from core.admin.resumen_registry import (
    Destino,
    Kpi,
    PeriodoResumen,
    ResumenProvider,
    resumen_registry,
)
from core.agents.spec import enabled_packs, load_active_spec
from core.agents.tolerante import KEY_GENERAL
from core.billing.tipos_de_uso import NO_ES_ACTIVIDAD
from core.db.models import (
    AgentInstanceRecord,
    AgentRecord,
    AppSettings,
    Conversation,
    ConversationTask,
    Escalation,
    UsageLedger,
)
from core.db.session import get_session
from core.metricas import fiabilidad
from core.shared.tenant import get_current_tenant_id
from core.shared.test_mode import TEST_CHANNEL

log = logging.getLogger(__name__)

DIAS_VALIDOS = (7, 15, 30)
ESTADOS_ABIERTOS = ("active", "suspended")
REF_GENERAL = KEY_GENERAL


@dataclass(frozen=True)
class Acceso:
    """Qué secciones puede ver el admin; el router lo calcula una vez con `effective_access`."""

    conversaciones: bool = False
    escalaciones: bool = False
    costos: bool = False
    agentes: bool = False
    # Secciones de módulo (`ResumenProvider.seccion`) que sí puede ver.
    modulos: frozenset[str] = field(default_factory=frozenset)


def periodo(ahora: datetime | None = None, dias: int = 7) -> PeriodoResumen:
    """Ventana de `dias` (7, 15 o 30) y la inmediata anterior de la misma duración."""
    if dias not in DIAS_VALIDOS:
        raise ValueError(f"dias fuera de rango: {dias}")
    hasta = ahora or datetime.now(UTC)
    ventana = timedelta(days=dias)
    desde = hasta - ventana
    return PeriodoResumen(
        desde=desde, hasta=hasta, previo_desde=desde - ventana, previo_hasta=desde
    )


def _versus(dias: int) -> str:
    return f"los {dias} días anteriores"


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("resumen: fuera del tenant_scope del negocio")


def _kpi_a_dict(k: Kpi, dias: int = 7) -> dict[str, Any]:
    return {
        "id": k.id,
        "label": k.label,
        "valor": k.valor,
        "unidad": k.unidad,
        "previo": k.previo,
        "delta_pct": k.delta_pct,
        "polaridad": k.polaridad,
        "hint": k.hint,
        "versus": k.versus or (_versus(dias) if k.previo is not None else None),
        "destino": {"ruta": k.destino.ruta, "params": dict(k.destino.params)}
        if k.destino
        else None,
    }


def _pct(parte: float | None) -> float | None:
    return None if parte is None else parte * 100


# ------------------------------------------------------------------ negocio (Coordinador)
async def _conversaciones(
    session: Any, tenant_id: uuid.UUID, desde: datetime, hasta: datetime
) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Conversation.id)).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.started_at >= desde,
                    Conversation.started_at < hasta,
                    Conversation.channel != TEST_CHANNEL,
                )
            )
        ).scalar()
        or 0
    )


async def _costo(session: Any, tenant_id: uuid.UUID, desde: datetime, hasta: datetime) -> float:
    return float(
        (
            await session.execute(
                select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.created_at >= desde,
                    UsageLedger.created_at < hasta,
                    UsageLedger.kind.not_in(NO_ES_ACTIVIDAD),
                )
            )
        ).scalar()
        or 0
    )


async def _general(
    session: Any, tenant_id: uuid.UUID, p: PeriodoResumen, acceso: Acceso
) -> list[Kpi]:
    kpis: list[Kpi] = []
    if acceso.conversaciones:
        kpis.append(
            Kpi(
                "conversaciones",
                "Conversaciones",
                float(await _conversaciones(session, tenant_id, p.desde, p.hasta)),
                previo=float(
                    await _conversaciones(session, tenant_id, p.previo_desde, p.previo_hasta)
                ),
                hint=f"Últimos {p.dias} días",
                destino=Destino("conversaciones"),
            )
        )
    if acceso.escalaciones:
        pendientes = (
            await session.execute(
                select(func.count(Escalation.id)).where(
                    Escalation.tenant_id == tenant_id, Escalation.status == "pendiente"
                )
            )
        ).scalar() or 0
        kpis.append(
            Kpi(
                "escalaciones_pendientes",
                "Escalaciones pendientes",
                float(pendientes),
                polaridad="up-bad",
                hint="Esperan a una persona",
                destino=Destino("escalaciones", {"estado": "pendiente"}),
            )
        )
    if acceso.agentes:
        ahora_op = await fiabilidad.operativas(
            session, tenant_id, fiabilidad.Filtro(desde=p.desde, hasta=p.hasta)
        )
        previo_op = await fiabilidad.operativas(
            session, tenant_id, fiabilidad.Filtro(desde=p.previo_desde, hasta=p.previo_hasta)
        )
        kpis.append(
            Kpi(
                "tasa_escalamiento",
                "Tasa de escalamiento",
                _pct(ahora_op.tasa_escalamiento),
                unidad="pct",
                previo=_pct(previo_op.tasa_escalamiento),
                polaridad="up-bad",
                hint=None if ahora_op.tasa_escalamiento is not None else "Sin datos suficientes",
                destino=Destino("agente", {"ref": REF_GENERAL, "pestana": "consumo"}),
            )
        )
        kpis.append(await _consistencia(session, tenant_id))
    if acceso.costos:
        kpis.append(
            Kpi(
                "costo_llm",
                "Costo de IA",
                await _costo(session, tenant_id, p.desde, p.hasta),
                unidad="usd",
                previo=await _costo(session, tenant_id, p.previo_desde, p.previo_hasta),
                polaridad="up-bad",
                hint=f"Últimos {p.dias} días",
                destino=Destino("costos"),
            )
        )
    return kpis


async def _consistencia(session: Any, tenant_id: uuid.UUID) -> Kpi:
    """Procedimientos dentro del umbral pass^k sobre los medidos, en todas las conexiones."""
    medidos = ok = 0
    for conexion in await fiabilidad.consistencia(session, tenant_id):
        for proc in conexion["procedimientos"]:
            if proc["pass_k"] is not None:
                medidos += 1
                ok += 1 if proc["ok"] else 0
    return Kpi(
        "consistencia",
        "Consistencia",
        ok / medidos * 100 if medidos else None,
        unidad="pct",
        hint="Procedimientos dentro del umbral" if medidos else "Sin mediciones todavía",
        destino=Destino("agente", {"ref": REF_GENERAL, "pestana": "consumo"}),
    )


# ------------------------------------------------------------------ por agente
async def _agentes(
    session: Any, tenant_id: uuid.UUID
) -> tuple[list[AgentRecord], dict[uuid.UUID, uuid.UUID]]:
    agentes = list(
        (
            await session.execute(
                select(AgentRecord)
                .where(
                    AgentRecord.tenant_id == tenant_id,
                    AgentRecord.deleted_at.is_(None),
                    AgentRecord.kind != "core",
                )
                .order_by(AgentRecord.created_at, AgentRecord.id)
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    filas = (
        await session.execute(
            select(AgentInstanceRecord.id, AgentInstanceRecord.agent_id).where(
                AgentInstanceRecord.tenant_id == tenant_id
            )
        )
    ).all()
    return agentes, {i: a for i, a in filas}


async def _actividad(
    session: Any, tenant_id: uuid.UUID, p: PeriodoResumen, agente_de: dict[uuid.UUID, uuid.UUID]
) -> dict[Any, dict[str, dict[str, float]]]:
    """Turnos, conversaciones y costo por agente (`None` = no atribuido) y ventana."""
    ventana = case((UsageLedger.created_at >= p.desde, "actual"), else_="previo")
    filas = (
        await session.execute(
            select(
                UsageLedger.agent_instance_id,
                ventana,
                func.count(UsageLedger.id),
                func.count(func.distinct(UsageLedger.conversation_id)),
                func.coalesce(func.sum(UsageLedger.cost_usd), 0),
            )
            .where(
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.created_at >= p.previo_desde,
                UsageLedger.created_at < p.hasta,
                UsageLedger.kind.not_in(NO_ES_ACTIVIDAD),
            )
            .group_by(UsageLedger.agent_instance_id, ventana)
        )
    ).all()
    # Las conversaciones distintas no se suman entre instancias de un mismo agente (una conversación
    # puede pasar por varias): es una aproximación por exceso, la misma que usa «Mi agente y su data».
    por_agente: dict[Any, dict[str, dict[str, float]]] = defaultdict(
        lambda: {
            v: {"turnos": 0.0, "conversaciones": 0.0, "costo": 0.0} for v in ("actual", "previo")
        }
    )
    for instancia, v, turnos, conversaciones, costo in filas:
        agente = agente_de.get(instancia) if instancia is not None else None
        acum = por_agente[agente][v]
        acum["turnos"] += float(turnos)
        acum["conversaciones"] += float(conversaciones)
        acum["costo"] += float(costo)
    return por_agente


async def _escalados(
    session: Any, tenant_id: uuid.UUID, p: PeriodoResumen, agente_de: dict[uuid.UUID, uuid.UUID]
) -> dict[Any, dict[str, float]]:
    """Turnos escalados por agente y ventana (solo runtime v3: es lo que escribe `turn_outcomes`)."""
    _exigir_scope(tenant_id)
    filas = (
        await session.execute(
            text(
                "SELECT coalesce(o.final_instance_id, d.active_instance_id), "
                "  CASE WHEN d.created_at >= :corte THEN 'actual' ELSE 'previo' END, count(*) "
                "FROM core_decisions d LEFT JOIN turn_outcomes o "
                "  ON o.tenant_id = d.tenant_id AND o.turn_id = d.id "
                "WHERE d.tenant_id = :t AND d.created_at >= :desde AND d.created_at < :hasta "
                "  AND d.outcome IN ('routed', 'escalated') "
                "  AND (d.outcome = 'escalated' OR o.status = 'escalated') GROUP BY 1, 2"
            ),
            {"t": tenant_id, "corte": p.desde, "desde": p.previo_desde, "hasta": p.hasta},
        )
    ).all()
    por: dict[Any, dict[str, float]] = defaultdict(lambda: {"actual": 0.0, "previo": 0.0})
    for instancia, v, n in filas:
        agente = agente_de.get(instancia) if instancia is not None else None
        por[agente][v] += float(n)
    return por


async def _tareas_abiertas(
    session: Any, tenant_id: uuid.UUID, agente_de: dict[uuid.UUID, uuid.UUID]
) -> dict[Any, float]:
    filas = (
        await session.execute(
            select(ConversationTask.owner_instance_id, func.count(ConversationTask.id))
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.status.in_(ESTADOS_ABIERTOS),
            )
            .group_by(ConversationTask.owner_instance_id)
        )
    ).all()
    por: dict[Any, float] = defaultdict(float)
    for instancia, n in filas:
        por[agente_de.get(instancia) if instancia is not None else None] += float(n)
    return por


async def _zona_horaria(session: Any, tenant_id: uuid.UUID) -> ZoneInfo:
    """Zona del negocio (la misma de la agenda) para cortar la serie por día calendario."""
    tz = (
        await session.execute(
            select(AppSettings.timezone).where(AppSettings.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    try:
        return ZoneInfo(tz or "America/Bogota")
    except Exception:  # una zona inválida no puede tumbar el Resumen
        return ZoneInfo("America/Bogota")


def _dias_del_periodo(p: PeriodoResumen, zona: ZoneInfo) -> list[date]:
    """Los N días calendario que terminan hoy en la zona del negocio."""
    fin = p.hasta.astimezone(zona).date()
    return [fin - timedelta(days=i) for i in range(p.dias - 1, -1, -1)]


async def _serie_diaria(
    session: Any,
    tenant_id: uuid.UUID,
    p: PeriodoResumen,
    agente_de: dict[uuid.UUID, uuid.UUID],
    zona: ZoneInfo,
) -> tuple[list[date], dict[Any, dict[date, dict[str, float]]], dict[Any, datetime]]:
    """Turnos, conversaciones y costo por agente y día (misma atribución que `_actividad`)."""
    dias = _dias_del_periodo(p, zona)
    desde = datetime.combine(dias[0], time.min, tzinfo=zona)
    dia = func.date_trunc(
        "day", func.timezone(str(zona), UsageLedger.created_at)
    ).label("dia")
    filas = (
        await session.execute(
            select(
                UsageLedger.agent_instance_id,
                dia,
                func.count(UsageLedger.id),
                func.count(func.distinct(UsageLedger.conversation_id)),
                func.coalesce(func.sum(UsageLedger.cost_usd), 0),
                func.max(UsageLedger.created_at),
            )
            .where(
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.created_at >= desde,
                UsageLedger.created_at < p.hasta,
                UsageLedger.kind.not_in(NO_ES_ACTIVIDAD),
            )
            .group_by(UsageLedger.agent_instance_id, dia)
        )
    ).all()
    puntos: dict[Any, dict[date, dict[str, float]]] = defaultdict(dict)
    ultima: dict[Any, datetime] = {}
    for instancia, fecha, turnos, conversaciones, costo, ultimo in filas:
        agente = agente_de.get(instancia) if instancia is not None else None
        clave_dia = fecha.date() if isinstance(fecha, datetime) else fecha
        if clave_dia < dias[0] or clave_dia > dias[-1]:
            continue
        puntos[agente][clave_dia] = {
            "turnos": float(turnos),
            "conversaciones": float(conversaciones),
            "costo": float(costo),
        }
        if ultimo is not None and (agente not in ultima or ultimo > ultima[agente]):
            ultima[agente] = ultimo
    return dias, puntos, ultima


def _serie_de(
    puntos: dict[Any, dict[date, dict[str, float]]],
    claves: list[Any],
    dias: list[date],
    ver_costo: bool,
) -> list[dict[str, Any]]:
    """Serie cero-rellena del agente; el General suma su instancia y lo no atribuido."""
    salida: list[dict[str, Any]] = []
    for dia in dias:
        turnos = conversaciones = costo = 0.0
        for clave in claves:
            fila = puntos.get(clave, {}).get(dia)
            if fila is None:
                continue
            turnos += fila["turnos"]
            conversaciones += fila["conversaciones"]
            costo += fila["costo"]
        salida.append(
            {
                "fecha": dia,
                "turnos": int(turnos),
                "conversaciones": int(conversaciones),
                "costo": round(costo, 6) if ver_costo else None,
            }
        )
    return salida


def _ultima_de(ultima: dict[Any, datetime], claves: list[Any]) -> datetime | None:
    fechas = [ultima[c] for c in claves if c in ultima]
    return max(fechas) if fechas else None


def _kpis_de_agente(
    ref: str,
    actual: dict[str, dict[str, float]],
    escalados: dict[str, float] | None,
    tareas: float,
    acceso: Acceso,
    dias: int,
) -> list[Kpi]:
    kpis = [
        Kpi(
            "turnos",
            "Turnos atendidos",
            actual["actual"]["turnos"],
            previo=actual["previo"]["turnos"],
            hint=f"Últimos {dias} días",
            destino=Destino("agente", {"ref": ref, "pestana": "conversaciones"}),
        ),
        Kpi(
            "conversaciones",
            "Conversaciones",
            actual["actual"]["conversaciones"],
            previo=actual["previo"]["conversaciones"],
            destino=Destino("agente", {"ref": ref, "pestana": "conversaciones"}),
        ),
    ]
    if escalados is not None:
        kpis.append(
            Kpi(
                "escalados",
                "Escalados a una persona",
                escalados["actual"],
                previo=escalados["previo"],
                polaridad="up-bad",
                destino=Destino("agente", {"ref": ref, "pestana": "conversaciones"}),
            )
        )
    kpis.append(
        Kpi(
            "tareas_abiertas",
            "Tareas abiertas",
            tareas,
            polaridad="up-bad",
            hint="En curso ahora",
            destino=Destino("agente", {"ref": ref, "pestana": "tareas"}),
        )
    )
    if acceso.costos:
        kpis.append(
            Kpi(
                "costo",
                "Costo de IA",
                actual["actual"]["costo"],
                unidad="usd",
                previo=actual["previo"]["costo"],
                polaridad="up-bad",
                destino=Destino("agente", {"ref": ref, "pestana": "consumo"}),
            )
        )
    return kpis


# ------------------------------------------------------------------ módulos (packs)
async def _modulo(
    provider: ResumenProvider, tenant_id: uuid.UUID, p: PeriodoResumen
) -> dict[str, Any]:
    try:
        kpis = await provider.kpis(tenant_id, p)
        return {
            "id": provider.plugin_id,
            "nombre": provider.nombre,
            "kpis": [_kpi_a_dict(k, p.dias) for k in kpis],
            "error": None,
        }
    except Exception:  # un pack caído no tumba el Resumen
        log.exception("resumen: falló el módulo %s", provider.plugin_id)
        return {
            "id": provider.plugin_id,
            "nombre": provider.nombre,
            "kpis": [],
            "error": "No se pudo cargar este módulo.",
        }


async def _habilitados() -> set[str]:
    try:
        return enabled_packs(await load_active_spec())
    except Exception:
        return set()


# ------------------------------------------------------------------ ensamble
async def resumen(
    tenant_id: uuid.UUID,
    acceso: Acceso,
    *,
    dias: int = 7,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """El Resumen por agente del negocio. El router decide `acceso`; aquí no hay permisos."""
    _exigir_scope(tenant_id)
    p = periodo(ahora, dias)
    habilitados = await _habilitados()
    providers = [
        x for x in resumen_registry.providers_for(habilitados) if x.seccion in acceso.modulos
    ]

    async with get_session() as session:
        runtime = (
            await session.execute(
                text("select aos_runtime from tenants where id = :t"), {"t": tenant_id}
            )
        ).scalar_one_or_none() or "legacy"
        general = await _general(session, tenant_id, p, acceso)
        agentes: list[AgentRecord] = []
        actividad: dict[Any, dict[str, dict[str, float]]] = {}
        escalados: dict[Any, dict[str, float]] = {}
        tareas: dict[Any, float] = {}
        dias_serie: list[date] = []
        puntos: dict[Any, dict[date, dict[str, float]]] = {}
        ultima: dict[Any, datetime] = {}
        if acceso.agentes:
            agentes, agente_de = await _agentes(session, tenant_id)
            actividad = await _actividad(session, tenant_id, p, agente_de)
            tareas = await _tareas_abiertas(session, tenant_id, agente_de)
            zona = await _zona_horaria(session, tenant_id)
            dias_serie, puntos, ultima = await _serie_diaria(
                session, tenant_id, p, agente_de, zona
            )
            if runtime == "v3":
                escalados = await _escalados(session, tenant_id, p, agente_de)

    claves = {a.key for a in agentes}
    # El General existe aunque falte su fila (negocio legacy sin backfill): absorbe lo no atribuido.
    filas: list[tuple[str, str, str, str, uuid.UUID | None]] = [
        (REF_GENERAL if a.key == KEY_GENERAL else str(a.id), a.key, a.name, a.kind, a.id)
        for a in agentes
    ]
    if acceso.agentes and KEY_GENERAL not in claves:
        filas.insert(0, (REF_GENERAL, KEY_GENERAL, "General", "specialist", None))

    huerfanos = [x for x in providers if x.agent_key not in claves]
    salida: list[dict[str, Any]] = []
    for ref, key, nombre, tipo, agente_id in filas:
        es_general = key == KEY_GENERAL
        vacio = {
            v: {"turnos": 0.0, "conversaciones": 0.0, "costo": 0.0} for v in ("actual", "previo")
        }
        sin_escalar = {"actual": 0.0, "previo": 0.0}
        # La fila virtual del General (`agente_id` None) no tiene datos propios: solo lo no atribuido.
        act = actividad.get(agente_id, vacio) if agente_id is not None else vacio
        esc = (
            (escalados.get(agente_id, sin_escalar) if agente_id is not None else sin_escalar)
            if runtime == "v3"
            else None
        )
        n_tareas = tareas.get(agente_id, 0.0) if agente_id is not None else 0.0
        if es_general:
            # Lo no atribuido (agente `None`) es del General.
            for v in ("actual", "previo"):
                extra = actividad.get(None, vacio)[v]
                act = {**act, v: {k: act[v][k] + extra[k] for k in act[v]}}
            if esc is not None:
                extra_e = escalados.get(None, sin_escalar)
                esc = {v: esc[v] + extra_e[v] for v in esc}
            n_tareas += tareas.get(None, 0.0)
        propios = [x for x in providers if x.agent_key == key] + (huerfanos if es_general else [])
        claves_serie = [agente_id] if agente_id is not None else []
        if es_general:
            claves_serie.append(None)
        salida.append(
            {
                "ref": ref,
                "key": key,
                "name": nombre,
                "kind": tipo,
                "kpis": [_kpi_a_dict(k, p.dias) for k in _kpis_de_agente(ref, act, esc, n_tareas, acceso, p.dias)],
                "modulos": [await _modulo(x, tenant_id, p) for x in propios],
                "serie": _serie_de(puntos, claves_serie, dias_serie, acceso.costos),
                "ultima_actividad": _ultima_de(ultima, claves_serie),
            }
        )

    return {
        "periodo": {
            "desde": p.desde,
            "hasta": p.hasta,
            "previo_desde": p.previo_desde,
            "previo_hasta": p.previo_hasta,
            "dias": p.dias,
        },
        "runtime": runtime,
        "general": {"kpis": [_kpi_a_dict(k, p.dias) for k in general]},
        "agentes": salida,
    }
