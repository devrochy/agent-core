"""Medición de cuotas por tenant (ADR-0045).

Las métricas se **derivan** de la tabla que ya es su fuente de verdad (no hay
un contador paralelo): `customers`, `usage_ledger`/`messages` (créditos, ADR-0144) y, desde
HU-BILL-05 (D-35, ADR-0142), `agents`/`agent_instances` y `tenant_user`/`tenant_invitations`, y desde HU-BILL-19
(ADR-0208 §8) `channel_connections` (`conexiones` y `correos`).
Cada métrica se declara una sola vez acá; el panel y el enforcement la consumen.

Desde los planes v3 (D-79, ADR-0208 §1, HU-BILL-18) el crédito es el único tope de consumo: los
topes de volumen (`conversaciones`, `conversaciones_audio`, `conversaciones_ocr`, `mensajes`,
`costo_llm_usd`) y `plugins` salieron del registro con sus conteos.

`count_metric()` corre dentro de `tenant_scope(tenant_id)` (RLS), así que sirve
tanto desde el webhook/consumer (que ya están en contexto) como desde el panel.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select

from core.db.models import (
    Admin,
    AgentInstanceRecord,
    AgentRecord,
    ChannelConnection,
    Customer,
    TenantInvitation,
    TenantUser,
)
from core.db.session import get_session
from core.shared.tenant import tenant_scope
from core.shared.test_mode import TEST_CHANNEL

# Vocabulario de `ChannelConnection.channel` (ADR-0208 §8-9). `conexiones` cuenta solo estos canales
# conversacionales: quedan fuera el correo (tiene su tope en `correos`), el Centro de pruebas
# (`TEST_CHANNEL`) y lo que no conversa (Google Calendar, `channel = ""`). WhatsApp cubre Meta, Twilio,
# Plivo y el vínculo por QR; el chat web es `web`.
CANALES_CONVERSACIONALES: frozenset[str] = frozenset({"whatsapp", "instagram", "messenger", "telegram", "web"})
CANAL_CORREO = "email"


def metrica_de_canal(channel: str | None) -> str | None:
    """La métrica estructural que consume una conexión de `channel`: `correos`, `conexiones` o None
    (el canal no tiene tope: Centro de pruebas, calendario)."""
    if channel == CANAL_CORREO:
        return "correos"
    if channel in CANALES_CONVERSACIONALES:
        return "conexiones"
    return None

MetricScope = Literal["period", "lifetime"]


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    scope: MetricScope
    description: str = ""


METRICS: tuple[Metric, ...] = (
    Metric(
        key="clientes",
        label="Clientes",
        unit="clientes",
        scope="lifetime",
        description="Clientes activos del tenant (total histórico).",
    ),
    Metric(
        key="agentes",
        label="Agentes publicados",
        unit="agentes",
        scope="lifetime",
        description=(
            "Agentes del negocio con al menos una instancia en una conexión (D-35). "
            "El Coordinador no cuenta."
        ),
    ),
    Metric(
        key="colaboradores",
        label="Colaboradores",
        unit="colaboradores",
        scope="lifetime",
        description="Cuentas activas con acceso al negocio sin el propietario, más invitaciones pendientes.",
    ),
    Metric(
        key="conexiones",
        label="Conexiones",
        unit="conexiones",
        scope="lifetime",
        description=(
            "Conexiones vivas y activas de canales conversacionales (WhatsApp, Instagram, Messenger, "
            "Telegram y chat web). No cuentan los correos ni la conexión del Centro de pruebas."
        ),
    ),
    Metric(
        key="correos",
        label="Correos conectados",
        unit="correos",
        scope="lifetime",
        description="Conexiones vivas y activas de correo (`channel = email`).",
    ),
    Metric(
        key="creditos",
        label="Crédito mensual",
        unit="créditos",
        scope="period",
        description=(
            "Créditos consumidos en el mes del ciclo (1 crédito = COP 10, ADR-0144): IA del "
            "usage_ledger más el fee de notas de voz, imágenes y plantillas."
        ),
    ),
)

METRICS_BY_KEY: dict[str, Metric] = {m.key: m for m in METRICS}
METRIC_KEYS: frozenset[str] = frozenset(METRICS_BY_KEY)


def get_metric(key: str) -> Metric | None:
    return METRICS_BY_KEY.get(key)


def metric_label(key: str) -> str:
    metric = METRICS_BY_KEY.get(key)
    return metric.label if metric else key


async def count_metric(
    tenant_id: uuid.UUID,
    key: str,
    *,
    period_start: datetime | None = None,
    session=None,
) -> int:
    """Consumo actual de una métrica para un tenant.

    Para métricas de período, `period_start` acota la ventana (el inicio del
    período de la suscripción). Devuelve 0 si la métrica no existe (nunca
    rompe el turno por una métrica mal escrita).

    `session`: si el caller YA tiene una sesión abierta dentro de su propio
    `tenant_scope`, la pasa para evitar abrir una sesión anidada (el
    `set_config(..., true)` de RLS es transaction-scoped y una sesión anidada
    sobre la misma conexión resetearía el contexto de tenant). Sin `session`,
    abre su propia sesión scoped.
    """
    metric = METRICS_BY_KEY.get(key)
    if metric is None:
        return 0

    if session is not None:
        return await _count_metric_in_session(session, tenant_id, key, period_start)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return await _count_metric_in_session(session, tenant_id, key, period_start)


async def _count_metric_in_session(session, tenant_id: uuid.UUID, key: str, period_start) -> int:
    if key == "clientes":
        return int(
            (
                await session.execute(
                    select(func.count(Customer.id)).where(
                        Customer.tenant_id == tenant_id,
                        Customer.deleted_at.is_(None),
                        # El cliente sintético del playground (channel "test") no
                        # es un cliente del negocio: no consume la métrica.
                        Customer.channel != TEST_CHANNEL,
                    )
                )
            ).scalar()
            or 0
        )
    if key == "creditos":
        # El llamador pasa el inicio del MES de consumo (`periodos.ventana_mensual`), no el
        # `period_start` crudo de la suscripción (ADR-0144). Décimas de crédito: float.
        from core.billing.creditos import medir_creditos

        return float((await medir_creditos(session, tenant_id, desde=period_start)).total)
    if key == "agentes":
        return await _count_agents(session, tenant_id)
    if key == "colaboradores":
        return await _count_collaborators(session, tenant_id)
    if key == "conexiones":
        return await _count_connections(session, tenant_id, CANALES_CONVERSACIONALES)
    if key == "correos":
        return await _count_connections(session, tenant_id, (CANAL_CORREO,))
    return 0


async def _count_connections(session, tenant_id: uuid.UUID, canales) -> int:
    """Conexiones vivas (`deleted_at` nulo) y activas del negocio en `canales` (ADR-0208 §8). Una
    pausada libera su cupo y reanudarla vuelve a medir el tope (`connections.set_active`). La
    canónica de un número de Meta cuenta aunque su cuenta esté pausada: no tiene estado propio
    (ADR-0122) y vuelve a responder al reanudar la cuenta."""
    return int(
        (
            await session.execute(
                select(func.count(ChannelConnection.id)).where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.deleted_at.is_(None),
                    ChannelConnection.active.is_(True),
                    ChannelConnection.channel.in_(tuple(canales)),
                )
            )
        ).scalar()
        or 0
    )


async def _count_agents(session, tenant_id: uuid.UUID) -> int:
    """Agentes publicados (D-35, ADR-0142, enmienda 2026-10-27): especialistas activos con al
    menos una instancia viva en una conexión viva. Se cuentan agentes, no filas de
    `agent_instances` (un agente en WhatsApp y en el chat web es uno). El especialista por defecto
    cuenta como cualquiera (ADR-0192 §7: sin General que no cuente); el Coordinador (`kind = core`)
    no, y el back-office (HU-AOS-37) es otra dimensión."""
    instanciado = (
        select(AgentInstanceRecord.id)
        .join(
            ChannelConnection,
            (ChannelConnection.id == AgentInstanceRecord.connection_id)
            & (ChannelConnection.tenant_id == AgentInstanceRecord.tenant_id),
        )
        .where(
            AgentInstanceRecord.tenant_id == tenant_id,
            AgentInstanceRecord.agent_id == AgentRecord.id,
            AgentInstanceRecord.deleted_at.is_(None),
            ChannelConnection.deleted_at.is_(None),
        )
        .exists()
    )
    query = select(func.count(AgentRecord.id)).where(
        AgentRecord.tenant_id == tenant_id,
        AgentRecord.kind == "specialist",
        AgentRecord.status == "active",
        AgentRecord.deleted_at.is_(None),
        instanciado,
    )
    return int((await session.execute(query)).scalar() or 0)


# Cuentas de plataforma (ADR-0134): un master o un tester vinculado no es un colaborador del
# negocio. Mismo conjunto que `core.admin.roles.ROLES_DE_PLATAFORMA` (no se importa para no
# acoplar billing con el paquete del panel).
_ROLES_DE_PLATAFORMA = ("master", "tester")


async def _count_collaborators(session, tenant_id: uuid.UUID) -> int:
    """Colaboradores (D-35, ADR-0142): cuentas **activas** vinculadas al negocio con rol
    `colaborador` (los propietarios no cuentan) más las invitaciones pendientes y vigentes a
    colaborador, para que no se puedan mandar N invitaciones estando a uno del tope. Desactivar
    una cuenta libera su cupo (decisión del dueño, 2026-10-19); reactivarla vuelve a medir el tope
    (`collaborators_service.toggle_active`)."""
    from sqlalchemy import or_

    vinculados = (
        await session.execute(
            select(func.count(TenantUser.id))
            .join(Admin, Admin.id == TenantUser.admin_id)
            .where(
                TenantUser.tenant_id == tenant_id,
                TenantUser.role == "colaborador",
                Admin.role.not_in(_ROLES_DE_PLATAFORMA),
                Admin.active.is_(True),
            )
        )
    ).scalar() or 0
    pendientes = (
        await session.execute(
            select(func.count(TenantInvitation.id)).where(
                TenantInvitation.tenant_id == tenant_id,
                TenantInvitation.role == "colaborador",
                TenantInvitation.estado == "pendiente",
                or_(TenantInvitation.expires_at.is_(None), TenantInvitation.expires_at > func.now()),
            )
        )
    ).scalar() or 0
    return int(vinculados) + int(pendientes)


async def usage_snapshot(
    tenant_id: uuid.UUID,
    *,
    period_start: datetime | None,
    limits: dict[str, int | None],
) -> list[dict]:
    """Consumo + límite + porcentaje por cada métrica con límite definido.

    Ordenado como `METRICS`. `limit=None` = ilimitado (`percent=None`). Es lo
    que alimenta las barras de progreso del panel.
    """
    rows: list[dict] = []
    for metric in METRICS:
        if metric.key not in limits:
            continue
        limit = limits.get(metric.key)
        used = await count_metric(tenant_id, metric.key, period_start=period_start)
        if limit is None:
            percent = None
        else:
            percent = 100.0 if limit == 0 else min(100.0, round(used * 100.0 / limit, 1))
        rows.append(
            {
                "key": metric.key,
                "label": metric.label,
                "unit": metric.unit,
                "scope": metric.scope,
                "used": used,
                "limit": limit,
                "percent": percent,
                # "excedido" es SUPERAR el límite, no estar justo en él: el plan
                # permite exactamente `limit` unidades, así que `used == limit`
                # (1/1, 5/5) NO es excedido. El bloqueo de la SIGUIENTE unidad
                # sí ocurre al llegar al tope -- eso lo decide
                # `enforcement.quota_decision` con `used >= limit`, no este flag.
                "exceeded": limit is not None and used > limit,
            }
        )
    return rows
