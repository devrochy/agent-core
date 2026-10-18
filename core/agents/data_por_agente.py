"""Lo que hizo y construyó cada agente del negocio (HU-AOS-47, ADR-0140). Solo lectura.

La unidad es el **agente** (`agents`), no la instancia: un agente tiene una instancia por conexión
y su data es la de todas ellas (filtrable por conexión). La atribución sale de lo que el runtime v3
ya escribe en cada turno:

- conversaciones, clientes y consumo: `usage_ledger.agent_instance_id` (el especialista en foco);
- tareas: `conversation_tasks.owner_instance_id`;
- registros (pedidos, leads, citas, promesas): `agent_record_origins` (0095).

**El General absorbe lo no atribuido** (`agent_instance_id IS NULL`): los turnos del runtime legacy
y los de antes de pasar a v3 los atendió el AgentSpec v2, que es el General. La respuesta lo marca
(`incluye_sin_atribuir`) para que el panel no lo presente como atribución exacta. El ref literal
`general` resuelve al agente `general` del negocio; si el negocio todavía no lo tiene (backfill sin
correr) o lo borró, a una vista del General que solo ve lo no atribuido.

Todo corre en el `tenant_scope` de la request (RLS) y además filtra `tenant_id` explícito. Un
agente de otro negocio es «no encontrado», sin eco del id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, false, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import array_agg

from core.agents.tolerante import KEY_GENERAL
from core.db.models import (
    AgentInstanceRecord,
    AgentRecord,
    AgentRecordOrigin,
    AgentVersionRecord,
    ChannelConnection,
    Conversation,
    ConversationTask,
    Customer,
    EvalRun,
    UsageLedger,
)
from core.db.session import get_session
from core.shared.test_mode import TEST_CHANNEL

REF_GENERAL = KEY_GENERAL
VENTANA_POR_DEFECTO = timedelta(days=30)
VENTANA_MAXIMA = timedelta(days=366)
MAX_DIAS_SERIE = 92
MAX_MEDICIONES = 10
ESTADOS_ABIERTOS = ("active", "suspended")
ESTADOS_CERRADOS = ("completed", "cancelled", "failed", "escalated")


class AgenteNoEncontrado(LookupError):
    """El agente no existe en el negocio activo (o es de otro): 404 sin eco."""


class PeriodoInvalido(ValueError):
    """`desde`/`hasta` fuera de rango; el mensaje es apto para el cliente."""


@dataclass(frozen=True)
class Alcance:
    """Qué filas son del agente: sus instancias (también las borradas, por la historia) y, para el
    General, lo no atribuido."""

    tenant_id: uuid.UUID
    agent_id: uuid.UUID | None
    key: str
    instancias: tuple[uuid.UUID, ...]
    incluye_sin_atribuir: bool

    def condicion(self, columna: Any) -> ColumnElement[bool]:
        partes: list[ColumnElement[bool]] = []
        if self.instancias:
            partes.append(columna.in_(self.instancias))
        if self.incluye_sin_atribuir:
            partes.append(columna.is_(None))
        return or_(*partes) if partes else false()


@dataclass(frozen=True)
class Periodo:
    desde: datetime
    hasta: datetime


@dataclass(frozen=True)
class Filtros:
    periodo: Periodo
    connection_id: uuid.UUID | None = None


@dataclass
class Pagina:
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0


def periodo(desde: datetime | None, hasta: datetime | None, *, ahora: datetime | None = None) -> Periodo:
    """Ventana `[desde, hasta)`: por defecto los últimos 30 días; máximo 366."""
    fin = _utc(hasta) if hasta is not None else (ahora or datetime.now(UTC))
    inicio = _utc(desde) if desde is not None else fin - VENTANA_POR_DEFECTO
    if inicio >= fin:
        raise PeriodoInvalido("`desde` debe ser anterior a `hasta`.")
    if fin - inicio > VENTANA_MAXIMA:
        raise PeriodoInvalido("El período no puede pasar de 366 días.")
    return Periodo(desde=inicio, hasta=fin)


def _utc(valor: datetime) -> datetime:
    return valor.replace(tzinfo=UTC) if valor.tzinfo is None else valor.astimezone(UTC)


# ------------------------------------------------------------------ resolución
async def resolver(tenant_id: uuid.UUID, ref: str) -> Alcance:
    """`ref` es el id de un agente del negocio o el literal `general`."""
    async with get_session() as session:
        if ref == REF_GENERAL:
            agente = (
                await session.execute(
                    select(AgentRecord).where(
                        AgentRecord.tenant_id == tenant_id,
                        AgentRecord.key == KEY_GENERAL,
                        AgentRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if agente is None:
                return Alcance(tenant_id, None, KEY_GENERAL, (), incluye_sin_atribuir=True)
        else:
            try:
                agent_id = uuid.UUID(ref)
            except ValueError:
                raise AgenteNoEncontrado from None
            agente = (
                await session.execute(
                    select(AgentRecord).where(
                        AgentRecord.tenant_id == tenant_id,
                        AgentRecord.id == agent_id,
                        AgentRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if agente is None:
                raise AgenteNoEncontrado
        instancias = (
            await session.execute(
                select(AgentInstanceRecord.id).where(
                    AgentInstanceRecord.tenant_id == tenant_id, AgentInstanceRecord.agent_id == agente.id
                )
            )
        ).scalars().all()
    return Alcance(
        tenant_id,
        agente.id,
        agente.key,
        tuple(instancias),
        incluye_sin_atribuir=agente.key == KEY_GENERAL,
    )


# ------------------------------------------------------------------ lista
async def resumen(tenant_id: uuid.UUID, *, ahora: datetime | None = None) -> dict[str, Any]:
    """Runtime, agentes (sin el Coordinador) con sus conexiones y actividad de 30 días, y lo no
    atribuido aparte."""
    desde = (ahora or datetime.now(UTC)) - VENTANA_POR_DEFECTO
    async with get_session() as session:
        runtime = (
            await session.execute(text("select aos_runtime from tenants where id = :t"), {"t": tenant_id})
        ).scalar_one_or_none() or "legacy"
        agentes = (
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
        ).scalars().all()
        instancias = (
            await session.execute(
                select(
                    AgentInstanceRecord.id,
                    AgentInstanceRecord.agent_id,
                    AgentInstanceRecord.connection_id,
                    AgentInstanceRecord.enabled,
                    AgentInstanceRecord.deleted_at,
                    ChannelConnection.label,
                    ChannelConnection.channel,
                    ChannelConnection.type_id,
                ).outerjoin(
                    ChannelConnection,
                    and_(
                        ChannelConnection.tenant_id == AgentInstanceRecord.tenant_id,
                        ChannelConnection.id == AgentInstanceRecord.connection_id,
                    ),
                )
                .where(AgentInstanceRecord.tenant_id == tenant_id)
            )
        ).all()
        actividad = (
            await session.execute(
                select(
                    UsageLedger.agent_instance_id,
                    func.count(UsageLedger.id),
                    func.count(func.distinct(UsageLedger.conversation_id)),
                    func.coalesce(func.sum(UsageLedger.cost_usd), 0),
                    func.max(UsageLedger.created_at),
                )
                .where(UsageLedger.tenant_id == tenant_id, UsageLedger.created_at >= desde)
                .group_by(UsageLedger.agent_instance_id)
            )
        ).all()
    por_instancia = {fila[0]: fila for fila in actividad}
    sin_atribuir = por_instancia.get(None)

    items: list[dict[str, Any]] = []
    tiene_general = False
    for agente in agentes:
        propias = [i for i in instancias if i.agent_id == agente.id]
        vivas = [i for i in propias if i.deleted_at is None]
        conexiones = [
            {"id": i.connection_id, "label": _etiqueta(i.label, i.channel, i.type_id), "enabled": bool(i.enabled)}
            for i in vivas
        ]
        filas = [por_instancia[i.id] for i in propias if i.id in por_instancia]
        es_general = agente.key == KEY_GENERAL
        tiene_general = tiene_general or es_general
        items.append(
            {
                "id": agente.id,
                "ref": REF_GENERAL if es_general else str(agente.id),
                "key": agente.key,
                "name": agente.name,
                "kind": agente.kind,
                "status": agente.status,
                "virtual": False,
                "published": any(i.enabled for i in vivas),
                "connections": conexiones,
                "incluye_sin_atribuir": es_general,
                **_sumar(filas + ([sin_atribuir] if es_general and sin_atribuir else [])),
            }
        )
    if not tiene_general:
        # Negocio sin el agente General (legacy sin backfill, o lo quitaron): su vista solo ve lo
        # no atribuido, sin romperse.
        items.insert(
            0,
            {
                "id": None,
                "ref": REF_GENERAL,
                "key": KEY_GENERAL,
                "name": "General",
                "kind": "specialist",
                "status": "active",
                "virtual": True,
                "published": False,
                "connections": [],
                "incluye_sin_atribuir": True,
                **_sumar([sin_atribuir] if sin_atribuir else []),
            },
        )
    return {"runtime": runtime, "items": items, "sin_atribuir": _sumar([sin_atribuir] if sin_atribuir else [])}


def _a_float(valor: object) -> float | None:
    """Numeric de Postgres (o None) a float; la consulta ya exige `pass_k` no nulo."""
    return float(valor) if isinstance(valor, (int, float, Decimal)) else None


def _etiqueta(label: str | None, channel: str | None, type_id: str | None) -> str:
    return (label or "").strip() or type_id or channel or "Conexión"


def _sumar(filas: list[Any]) -> dict[str, Any]:
    return {
        "turns": int(sum(f[1] for f in filas)),
        "conversations": int(sum(f[2] for f in filas)),
        "cost_usd": float(sum(float(f[3] or 0) for f in filas)),
        "last_activity_at": max((f[4] for f in filas if f[4] is not None), default=None),
    }


# ------------------------------------------------------------------ conversaciones y clientes
def _turnos_del_agente(alcance: Alcance, filtros: Filtros) -> list[ColumnElement[bool]]:
    condiciones: list[ColumnElement[bool]] = [
        UsageLedger.tenant_id == alcance.tenant_id,
        alcance.condicion(UsageLedger.agent_instance_id),
        UsageLedger.created_at >= filtros.periodo.desde,
        UsageLedger.created_at < filtros.periodo.hasta,
        UsageLedger.conversation_id.is_not(None),
        Conversation.tenant_id == alcance.tenant_id,
        Conversation.deleted_at.is_(None),
        Conversation.channel != TEST_CHANNEL,
    ]
    if filtros.connection_id is not None:
        condiciones.append(
            func.coalesce(UsageLedger.connection_id, Conversation.connection_id) == filtros.connection_id
        )
    return condiciones


async def conversaciones(alcance: Alcance, filtros: Filtros, *, offset: int, limit: int) -> Pagina:
    union = Conversation.id == UsageLedger.conversation_id
    condiciones = _turnos_del_agente(alcance, filtros)
    ultimo = func.max(UsageLedger.created_at).label("ultimo")
    consulta = (
        select(
            UsageLedger.conversation_id,
            func.count(UsageLedger.id),
            func.coalesce(func.sum(UsageLedger.cost_usd), 0),
            ultimo,
            array_agg(UsageLedger.agent_version_id, order_by=UsageLedger.created_at.desc())[1],
            array_agg(
                func.coalesce(UsageLedger.connection_id, Conversation.connection_id),
                order_by=UsageLedger.created_at.desc(),
            )[1],
        )
        .join(Conversation, union)
        .where(*condiciones)
        .group_by(UsageLedger.conversation_id)
    )
    async with get_session() as session:
        total = (
            await session.execute(
                select(func.count(func.distinct(UsageLedger.conversation_id)))
                .select_from(UsageLedger)
                .join(Conversation, union)
                .where(*condiciones)
            )
        ).scalar_one()
        filas = (
            await session.execute(
                consulta.order_by(ultimo.desc(), UsageLedger.conversation_id).offset(offset).limit(limit)
            )
        ).all()
        convs = await _conversaciones_con_cliente(
            session, alcance.tenant_id, [c for f in filas if (c := f[0]) is not None]
        )
        versiones = await _numeros_de_version(session, alcance.tenant_id, [f[4] for f in filas if f[4]])
        conexiones = await _etiquetas_de_conexion(session, alcance.tenant_id, [f[5] for f in filas if f[5]])
    items = []
    for conv_id, turnos, costo, ult, version_id, conn_id in filas:
        if conv_id is None:  # la consulta exige `conversation_id`; guarda para el tipo
            continue
        conv = convs.get(conv_id)
        items.append(
            {
                "conversation_id": conv_id,
                "customer_id": conv["customer_id"] if conv else None,
                "customer_name": conv["customer_name"] if conv else None,
                "channel": conv["channel"] if conv else None,
                "status": conv["status"] if conv else None,
                "connection_id": conn_id,
                "connection_label": conexiones.get(conn_id) if conn_id else None,
                "agent_version": versiones.get(version_id) if version_id else None,
                "turns": int(turnos),
                "cost_usd": float(costo or 0),
                "last_turn_at": ult,
            }
        )
    return Pagina(items=items, total=int(total))


async def clientes(alcance: Alcance, filtros: Filtros, *, offset: int, limit: int) -> Pagina:
    union = Conversation.id == UsageLedger.conversation_id
    condiciones = _turnos_del_agente(alcance, filtros)
    ultimo = func.max(UsageLedger.created_at).label("ultimo")
    async with get_session() as session:
        total = (
            await session.execute(
                select(func.count(func.distinct(Conversation.customer_id)))
                .select_from(UsageLedger)
                .join(Conversation, union)
                .where(*condiciones)
            )
        ).scalar_one()
        filas = (
            await session.execute(
                select(
                    Conversation.customer_id,
                    func.count(func.distinct(UsageLedger.conversation_id)),
                    func.count(UsageLedger.id),
                    ultimo,
                )
                .select_from(UsageLedger)
                .join(Conversation, union)
                .where(*condiciones)
                .group_by(Conversation.customer_id)
                .order_by(ultimo.desc(), Conversation.customer_id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        ids = [f[0] for f in filas if f[0] is not None]
        nombres: dict[uuid.UUID, tuple[str | None, str | None]] = {}
        if ids:
            for cid, nombre, canal in (
                await session.execute(
                    select(Customer.id, Customer.name, Customer.channel).where(
                        Customer.tenant_id == alcance.tenant_id, Customer.id.in_(ids)
                    )
                )
            ).all():
                nombres[cid] = (nombre, canal)
    return Pagina(
        items=[
            {
                "customer_id": cid,
                "name": nombres.get(cid, (None, None))[0],
                "channel": nombres.get(cid, (None, None))[1],
                "conversations": int(n_conv),
                "turns": int(n_turnos),
                "last_turn_at": ult,
            }
            for cid, n_conv, n_turnos, ult in filas
        ],
        total=int(total),
    )


async def _conversaciones_con_cliente(session: Any, tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not ids:
        return {}
    filas = (
        await session.execute(
            select(Conversation.id, Conversation.customer_id, Conversation.channel, Conversation.status, Customer.name)
            .outerjoin(Customer, and_(Customer.tenant_id == Conversation.tenant_id, Customer.id == Conversation.customer_id))
            .where(Conversation.tenant_id == tenant_id, Conversation.id.in_(ids))
        )
    ).all()
    return {
        f[0]: {"customer_id": f[1], "channel": f[2], "status": f[3], "customer_name": f[4]} for f in filas
    }


async def _numeros_de_version(session: Any, tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    filas = (
        await session.execute(
            select(AgentVersionRecord.id, AgentVersionRecord.version).where(
                AgentVersionRecord.tenant_id == tenant_id, AgentVersionRecord.id.in_(set(ids))
            )
        )
    ).all()
    return {f[0]: int(f[1]) for f in filas}


async def _etiquetas_de_conexion(session: Any, tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    filas = (
        await session.execute(
            select(ChannelConnection.id, ChannelConnection.label, ChannelConnection.channel, ChannelConnection.type_id).where(
                ChannelConnection.tenant_id == tenant_id, ChannelConnection.id.in_(set(ids))
            )
        )
    ).all()
    return {f[0]: _etiqueta(f[1], f[2], f[3]) for f in filas}


# ------------------------------------------------------------------ tareas
async def tareas(
    alcance: Alcance, filtros: Filtros, *, estado: str | None, offset: int, limit: int
) -> Pagina:
    condiciones: list[ColumnElement[bool]] = [
        ConversationTask.tenant_id == alcance.tenant_id,
        alcance.condicion(ConversationTask.owner_instance_id),
        ConversationTask.updated_at >= filtros.periodo.desde,
        ConversationTask.updated_at < filtros.periodo.hasta,
        Conversation.tenant_id == alcance.tenant_id,
        Conversation.channel != TEST_CHANNEL,
        Conversation.deleted_at.is_(None),
    ]
    if estado == "open":
        condiciones.append(ConversationTask.status.in_(ESTADOS_ABIERTOS))
    elif estado == "closed":
        condiciones.append(ConversationTask.status.in_(ESTADOS_CERRADOS))
    if filtros.connection_id is not None:
        condiciones.append(Conversation.connection_id == filtros.connection_id)
    union = Conversation.id == ConversationTask.conversation_id
    async with get_session() as session:
        total = (
            await session.execute(
                select(func.count(ConversationTask.id)).select_from(ConversationTask).join(Conversation, union).where(*condiciones)
            )
        ).scalar_one()
        filas = (
            await session.execute(
                select(
                    ConversationTask.id,
                    ConversationTask.conversation_id,
                    ConversationTask.procedure_id,
                    ConversationTask.procedure_version,
                    ConversationTask.status,
                    ConversationTask.status_reason,
                    ConversationTask.stage,
                    ConversationTask.created_at,
                    ConversationTask.updated_at,
                    ConversationTask.completed_at,
                    Conversation.connection_id,
                )
                .join(Conversation, union)
                .where(*condiciones)
                .order_by(ConversationTask.updated_at.desc(), ConversationTask.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    return Pagina(
        items=[
            {
                "id": f[0],
                "conversation_id": f[1],
                "procedure_id": f[2],
                "procedure_version": f[3],
                "status": f[4],
                "open": f[4] in ESTADOS_ABIERTOS,
                "status_reason": f[5],
                "stage": f[6],
                "created_at": f[7],
                "updated_at": f[8],
                "completed_at": f[9],
                "connection_id": f[10],
            }
            for f in filas
        ],
        total=int(total),
    )


# ------------------------------------------------------------------ registros
async def registros(
    alcance: Alcance,
    filtros: Filtros,
    *,
    packs_visibles: frozenset[str],
    kind: str | None,
    offset: int,
    limit: int,
) -> Pagina:
    if not packs_visibles:
        return Pagina()
    condiciones: list[ColumnElement[bool]] = [
        AgentRecordOrigin.tenant_id == alcance.tenant_id,
        alcance.condicion(AgentRecordOrigin.agent_instance_id),
        AgentRecordOrigin.created_at >= filtros.periodo.desde,
        AgentRecordOrigin.created_at < filtros.periodo.hasta,
        AgentRecordOrigin.pack_id.in_(sorted(packs_visibles)),
        or_(Conversation.id.is_(None), Conversation.channel != TEST_CHANNEL),
    ]
    if kind is not None:
        condiciones.append(AgentRecordOrigin.kind == kind)
    if filtros.connection_id is not None:
        condiciones.append(AgentRecordOrigin.connection_id == filtros.connection_id)
    union = and_(
        Conversation.tenant_id == AgentRecordOrigin.tenant_id, Conversation.id == AgentRecordOrigin.conversation_id
    )
    async with get_session() as session:
        total = (
            await session.execute(
                select(func.count(AgentRecordOrigin.id))
                .select_from(AgentRecordOrigin)
                .outerjoin(Conversation, union)
                .where(*condiciones)
            )
        ).scalar_one()
        filas = (
            await session.execute(
                select(AgentRecordOrigin)
                .outerjoin(Conversation, union)
                .where(*condiciones)
                .order_by(AgentRecordOrigin.created_at.desc(), AgentRecordOrigin.id)
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()
        versiones = await _numeros_de_version(
            session, alcance.tenant_id, [f.agent_version_id for f in filas if f.agent_version_id]
        )
        conexiones = await _etiquetas_de_conexion(
            session, alcance.tenant_id, [f.connection_id for f in filas if f.connection_id]
        )
    return Pagina(
        items=[
            {
                "id": f.id,
                "pack_id": f.pack_id,
                "kind": f.kind,
                "record_ref": f.record_ref,
                "label": f.label,
                "tool": f.tool,
                "conversation_id": f.conversation_id,
                "connection_id": f.connection_id,
                "connection_label": conexiones.get(f.connection_id) if f.connection_id else None,
                "agent_version": versiones.get(f.agent_version_id) if f.agent_version_id else None,
                "created_at": f.created_at,
            }
            for f in filas
        ],
        total=int(total),
    )


# ------------------------------------------------------------------ consumo y consistencia
async def consumo(alcance: Alcance, filtros: Filtros) -> dict[str, Any]:
    condiciones: list[ColumnElement[bool]] = [
        UsageLedger.tenant_id == alcance.tenant_id,
        alcance.condicion(UsageLedger.agent_instance_id),
        UsageLedger.created_at >= filtros.periodo.desde,
        UsageLedger.created_at < filtros.periodo.hasta,
    ]
    if filtros.connection_id is not None:
        condiciones.append(UsageLedger.connection_id == filtros.connection_id)
    dia = func.date_trunc(literal("day"), func.timezone(literal("UTC"), UsageLedger.created_at)).label("dia")
    sumas = (
        func.count(UsageLedger.id),
        func.coalesce(func.sum(UsageLedger.input_tokens), 0),
        func.coalesce(func.sum(UsageLedger.output_tokens), 0),
        func.coalesce(func.sum(UsageLedger.cost_usd), 0),
    )
    async with get_session() as session:
        total = (await session.execute(select(*sumas).where(*condiciones))).one()
        por_conexion = (
            await session.execute(
                select(UsageLedger.connection_id, *sumas)
                .where(*condiciones)
                .group_by(UsageLedger.connection_id)
                .order_by(sumas[3].desc())
                .limit(50)
            )
        ).all()
        por_dia = (
            await session.execute(
                select(dia, *sumas).where(*condiciones).group_by(dia).order_by(dia.desc()).limit(MAX_DIAS_SERIE)
            )
        ).all()
        etiquetas = await _etiquetas_de_conexion(
            session, alcance.tenant_id, [c for f in por_conexion if (c := f[0]) is not None]
        )
        mediciones = (
            await session.execute(
                select(EvalRun.created_at, EvalRun.k, EvalRun.pass_1, EvalRun.pass_k, EvalRun.spec_version)
                .where(
                    EvalRun.tenant_id == alcance.tenant_id,
                    EvalRun.deleted_at.is_(None),
                    EvalRun.k > 1,
                    EvalRun.pass_k.is_not(None),
                )
                .order_by(EvalRun.created_at.desc())
                .limit(MAX_MEDICIONES)
            )
        ).all()

    def _fila(f: Any, desde: int = 0) -> dict[str, Any]:
        return {
            "turns": int(f[desde]),
            "input_tokens": int(f[desde + 1]),
            "output_tokens": int(f[desde + 2]),
            "cost_usd": float(f[desde + 3] or 0),
        }

    return {
        "periodo": {"desde": filtros.periodo.desde, "hasta": filtros.periodo.hasta},
        "incluye_sin_atribuir": alcance.incluye_sin_atribuir,
        "total": _fila(total),
        "by_connection": [
            {
                "connection_id": f[0],
                "connection_label": etiquetas.get(c) if (c := f[0]) is not None else None,
                **_fila(f, 1),
            }
            for f in por_conexion
        ],
        "by_day": [{"day": f[0].date(), **_fila(f, 1)} for f in reversed(por_dia)],
        # pass^k es del negocio (eval_runs no se atribuye a un agente todavía): el panel lo dice.
        "consistency": {
            "scope": "negocio",
            "measurements": [
                {
                    "measured_at": m[0],
                    "k": int(m[1]),
                    "pass_1": _a_float(m[2]),
                    "pass_k": _a_float(m[3]) or 0.0,
                    "spec_version": m[4],
                }
                for m in mediciones
            ],
        },
    }
