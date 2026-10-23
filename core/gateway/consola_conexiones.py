"""Consola de conexiones (HU-AOS-23, SPEC-26 §6.2 a): lectura de solo lectura, por conexión.

Una fila por `channel_connections` viva del negocio (manuales, la canónica de cada número de
Meta, ADR-0115, y el chat web), que es la fila a la que apuntan `agent_instances`,
`connection_releases` y `usage_ledger.connection_id`. Las cuentas de Meta (`meta_connections`)
no son filas propias: aportan su estado (salud y pausa, ADR-0122) a sus canónicas.

Por fila: Coordinador y agentes vivos con la versión que corre en la release activa
(`release_components`) y la última publicada; release activa y canary; salud; consumo de los
últimos 7 días (`usage_ledger`, índice parcial `ix_usage_ledger_conn` de 0071). Todas las
consultas van en lote por los ids de la página, nunca una por fila.

Siempre dentro del `tenant_scope` del negocio (RLS) y además filtra por `tenant_id`.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text

from core.db.models import utcnow
from core.db.session import get_session
from core.kernel.banderas import banderas_del_negocio
from core.shared.tenant import get_current_tenant_id

VENTANA_CONSUMO = timedelta(days=7)
MAX_SIN_CONEXION = 50

# Pausa que aplica a cada fila (lo que el panel ofrece y a qué endpoint va).
PAUSA_CUENTA_META = "meta_account"  # ADR-0122: alcanza a todos los números de la cuenta
# Manual, WhatsApp Web o chat web: `active = false` (`/admin/connections/{id}/action`). Corta la
# entrada y la salida igual que la cuenta de Meta (adendas 2026-10-19 de ADR-0122, ADR-0115 §2).
# En el chat web es el mismo interruptor que Configuración → Chat web
# (`chat_web_conexion.establecer_habilitado`).
PAUSA_CONEXION = "connection"
# Ya no se emite (el chat web se pausa con `connection`); queda en el enum de la API para no romper
# a los clientes generados.
PAUSA_CHAT_WEB = "webchat_settings"


@dataclass(frozen=True)
class AgenteFila:
    instance_id: uuid.UUID
    agent_id: uuid.UUID
    key: str
    name: str
    role: str
    is_default: bool
    enabled: bool
    priority: int
    release_version: int | None
    latest_version: int | None


@dataclass(frozen=True)
class ReleaseFila:
    id: uuid.UUID
    number: int
    created_at: datetime


@dataclass(frozen=True)
class ConsumoFila:
    turns: int = 0
    conversations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ConexionFila:
    id: uuid.UUID
    channel: str
    type_id: str | None
    label: str
    external_id: str
    conversational: bool
    active: bool
    is_primary: bool
    created_at: datetime | None
    meta_connection_id: uuid.UUID | None
    meta_status: str | None
    health: str
    pause_scope: str
    # La consola ofrece la pausa que detiene de verdad: la de la cuenta de Meta, la de una conexión
    # manual o de WhatsApp Web y la del chat web (mismo estado que Configuración → Chat web).
    pausable: bool
    agents: list[AgenteFila] = field(default_factory=list)
    active_release: ReleaseFila | None = None
    canary_release: ReleaseFila | None = None
    usage_7d: ConsumoFila = ConsumoFila()


@dataclass(frozen=True)
class AgenteSinConexion:
    id: uuid.UUID
    key: str
    name: str


@dataclass(frozen=True)
class Consistencia:
    pass_k: float
    k: int
    measured_at: datetime


@dataclass(frozen=True)
class Consola:
    items: list[ConexionFila]
    total: int
    runtime: str
    routing: str
    consistency: Consistencia | None
    unassigned_agents: list[AgenteSinConexion]


def salud(*, active: bool, meta_status: str | None, tiene_cuenta: bool) -> str:
    """`ok | degraded | error | paused | pending`. Con cuenta de Meta manda su estado (ADR-0122:
    `revoked` es la pausa); sin cuenta, `active`."""
    if tiene_cuenta:
        return {
            "connected": "ok",
            "degraded": "degraded",
            "error": "error",
            "revoked": "paused",
            "draft": "pending",
            "pending": "pending",
        }.get(meta_status or "", "error")
    return "ok" if active else "paused"


def alcance_de_pausa(*, tiene_cuenta: bool, type_id: str | None) -> str:
    del (
        type_id
    )  # el chat web también es una conexión (pausa propia, sincronizada con Configuración)
    return PAUSA_CUENTA_META if tiene_cuenta else PAUSA_CONEXION


def pausable(*, tiene_cuenta: bool, meta_status: str | None, type_id: str | None) -> bool:
    """¿La consola ofrece Pausar/Reanudar? La cuenta de Meta (si la fila tiene estado) y toda
    conexión sin cuenta: manual, WhatsApp Web y chat web (este, sincronizado con Configuración)."""
    if tiene_cuenta:
        return meta_status is not None
    return alcance_de_pausa(tiene_cuenta=False, type_id=type_id) == PAUSA_CONEXION


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("consola de conexiones: fuera del tenant_scope del negocio")


async def consola(tenant_id: uuid.UUID, *, offset: int, limite: int) -> Consola:
    _exigir_scope(tenant_id)
    async with get_session() as session:
        banderas = await banderas_del_negocio(session, tenant_id)
        total = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM channel_connections cc "
                        "LEFT JOIN meta_connections mc "
                        "  ON mc.tenant_id = cc.tenant_id AND mc.id = cc.meta_connection_id AND mc.deleted_at IS NULL "
                        "WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL "
                        "  AND (cc.meta_connection_id IS NULL OR mc.id IS NOT NULL)"
                    ),
                    {"t": tenant_id},
                )
            ).scalar_one()
        )
        filas = (
            await session.execute(
                text(
                    "SELECT cc.id, cc.channel, cc.type_id, cc.label, cc.external_id, "
                    "  coalesce(ct.produces_conversations, true), cc.active, cc.is_primary, cc.created_at, "
                    "  cc.meta_connection_id, mc.status, cc.active_release_id, cc.canary_release_id "
                    "FROM channel_connections cc "
                    "LEFT JOIN connection_types ct ON ct.id = cc.type_id "
                    "LEFT JOIN meta_connections mc "
                    "  ON mc.tenant_id = cc.tenant_id AND mc.id = cc.meta_connection_id AND mc.deleted_at IS NULL "
                    "WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL "
                    "  AND (cc.meta_connection_id IS NULL OR mc.id IS NOT NULL) "
                    "ORDER BY coalesce(ct.produces_conversations, true) DESC, cc.created_at, cc.id "
                    "OFFSET :o LIMIT :l"
                ),
                {"t": tenant_id, "o": offset, "l": limite},
            )
        ).all()
        ids = [f[0] for f in filas]
        releases_ids = [r for f in filas for r in (f[11], f[12]) if r is not None]
        agentes = await _agentes(session, tenant_id, ids)
        releases = await _releases(session, tenant_id, releases_ids)
        consumo = await _consumo(session, tenant_id, ids)
        consistencia = await _consistencia(session, tenant_id)
        sin_conexion = await _publicados_sin_conexion(session, tenant_id)

    items: list[ConexionFila] = []
    for f in filas:
        cid, tiene_cuenta = f[0], f[9] is not None
        activa = f[11]
        items.append(
            ConexionFila(
                id=cid,
                channel=f[1],
                type_id=f[2],
                label=f[3] or f[4],
                external_id=f[4],
                conversational=bool(f[5]),
                active=bool(f[6]),
                is_primary=bool(f[7]),
                created_at=f[8],
                meta_connection_id=f[9],
                meta_status=f[10],
                health=salud(active=bool(f[6]), meta_status=f[10], tiene_cuenta=tiene_cuenta),
                pause_scope=alcance_de_pausa(tiene_cuenta=tiene_cuenta, type_id=f[2]),
                pausable=pausable(tiene_cuenta=tiene_cuenta, meta_status=f[10], type_id=f[2]),
                agents=[_con_version(a, activa) for a in agentes.get(cid, [])],
                active_release=releases.get(activa) if activa is not None else None,
                canary_release=releases.get(f[12]) if f[12] is not None else None,
                usage_7d=consumo.get(cid, ConsumoFila()),
            )
        )
    return Consola(
        items=items,
        total=total,
        runtime=banderas.runtime,
        routing=banderas.ruteo,
        consistency=consistencia,
        unassigned_agents=sin_conexion,
    )


def _con_version(a: dict[str, Any], activa: uuid.UUID | None) -> AgenteFila:
    return AgenteFila(
        instance_id=a["instance_id"],
        agent_id=a["agent_id"],
        key=a["key"],
        name=a["name"],
        role=a["role"],
        is_default=a["is_default"],
        enabled=a["enabled"],
        priority=a["priority"],
        release_version=a["por_release"].get(activa) if activa is not None else None,
        latest_version=a["latest_version"],
    )


async def _agentes(
    session: Any, tenant_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Instancias vivas por conexión (Coordinador primero, luego por prioridad), con la versión de
    cada release donde figuran (para elegir la de la activa)."""
    if not ids:
        return {}
    filas = (
        await session.execute(
            text(
                "SELECT i.connection_id, i.id, i.agent_id, a.key, a.name, i.role, i.is_default, i.enabled, i.priority, "
                "  (SELECT max(v.version) FROM agent_versions v WHERE v.tenant_id = i.tenant_id AND v.agent_id = i.agent_id) "
                "FROM agent_instances i "
                "JOIN agents a ON a.tenant_id = i.tenant_id AND a.id = i.agent_id "
                "WHERE i.tenant_id = :t AND i.connection_id IN :ids AND i.deleted_at IS NULL "
                "ORDER BY i.connection_id, (i.role <> 'core'), i.priority, a.key"
            ).bindparams(bindparam("ids", expanding=True)),
            {"t": tenant_id, "ids": ids},
        )
    ).all()
    componentes = (
        await session.execute(
            text(
                "SELECT rc.agent_instance_id, rc.release_id, v.version "
                "FROM release_components rc "
                "JOIN channel_connections cc ON cc.tenant_id = rc.tenant_id AND cc.id = rc.connection_id "
                "JOIN agent_versions v ON v.tenant_id = rc.tenant_id AND v.id = rc.agent_version_id "
                "WHERE rc.tenant_id = :t AND rc.connection_id IN :ids AND rc.release_id = cc.active_release_id"
            ).bindparams(bindparam("ids", expanding=True)),
            {"t": tenant_id, "ids": ids},
        )
    ).all()
    por_instancia: dict[uuid.UUID, dict[uuid.UUID, int]] = defaultdict(dict)
    for iid, rid, version in componentes:
        por_instancia[iid][rid] = int(version)
    salida: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for f in filas:
        salida[f[0]].append(
            {
                "instance_id": f[1],
                "agent_id": f[2],
                "key": f[3],
                "name": f[4],
                "role": f[5],
                "is_default": bool(f[6]),
                "enabled": bool(f[7]),
                "priority": int(f[8]),
                "latest_version": int(f[9]) if f[9] is not None else None,
                "por_release": por_instancia.get(f[1], {}),
            }
        )
    return salida


async def _releases(
    session: Any, tenant_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, ReleaseFila]:
    if not ids:
        return {}
    filas = (
        await session.execute(
            text(
                "SELECT id, number, created_at FROM connection_releases WHERE tenant_id = :t AND id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"t": tenant_id, "ids": ids},
        )
    ).all()
    return {f[0]: ReleaseFila(f[0], int(f[1]), f[2]) for f in filas}


async def _consumo(
    session: Any, tenant_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, ConsumoFila]:
    """Últimos 7 días. Con `aos_runtime = legacy` el loop escribe `connection_id` NULL: da cero."""
    if not ids:
        return {}
    filas = (
        await session.execute(
            text(
                "SELECT connection_id, count(DISTINCT coalesce(turn_id, id)), count(DISTINCT conversation_id), "
                "  coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0), coalesce(sum(cost_usd), 0) "
                "FROM usage_ledger "
                "WHERE tenant_id = :t AND connection_id IS NOT NULL AND connection_id IN :ids AND created_at >= :desde "
                "GROUP BY connection_id"
            ).bindparams(bindparam("ids", expanding=True)),
            {"t": tenant_id, "ids": ids, "desde": utcnow() - VENTANA_CONSUMO},
        )
    ).all()
    return {
        f[0]: ConsumoFila(int(f[1]), int(f[2]), int(f[3]), int(f[4]), float(f[5])) for f in filas
    }


async def _consistencia(session: Any, tenant_id: uuid.UUID) -> Consistencia | None:
    """pass^k del último run del NEGOCIO (no hay medición por conexión todavía)."""
    fila = (
        await session.execute(
            text(
                "SELECT pass_k, k, started_at FROM eval_runs "
                "WHERE tenant_id = :t AND deleted_at IS NULL AND pass_k IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1"
            ),
            {"t": tenant_id},
        )
    ).first()
    if fila is None:
        return None
    return Consistencia(float(fila[0]), int(fila[1]), fila[2])


async def _publicados_sin_conexion(session: Any, tenant_id: uuid.UUID) -> list[AgenteSinConexion]:
    """D-35: un agente publicado debe tener al menos una conexión. Agentes vivos y activos, no
    Coordinador, con versión publicada y sin instancia viva en ninguna conexión viva."""
    filas = (
        await session.execute(
            text(
                "SELECT a.id, a.key, a.name FROM agents a "
                "WHERE a.tenant_id = :t AND a.deleted_at IS NULL AND a.status = 'active' AND a.kind <> 'core' "
                "  AND EXISTS (SELECT 1 FROM agent_versions v WHERE v.tenant_id = a.tenant_id AND v.agent_id = a.id) "
                "  AND NOT EXISTS (SELECT 1 FROM agent_instances i "
                "    JOIN channel_connections cc ON cc.tenant_id = i.tenant_id AND cc.id = i.connection_id "
                "    WHERE i.tenant_id = a.tenant_id AND i.agent_id = a.id "
                "      AND i.deleted_at IS NULL AND cc.deleted_at IS NULL) "
                "ORDER BY a.key LIMIT :l"
            ),
            {"t": tenant_id, "l": MAX_SIN_CONEXION},
        )
    ).all()
    return [AgenteSinConexion(f[0], f[1], f[2]) for f in filas]


__all__ = [
    "PAUSA_CHAT_WEB",
    "PAUSA_CONEXION",
    "PAUSA_CUENTA_META",
    "Consola",
    "alcance_de_pausa",
    "consola",
    "salud",
]
