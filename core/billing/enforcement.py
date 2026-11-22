"""Enforcement de planes y cuotas (ADR-0045).

Dos niveles, ambos deterministas y calculados en vivo:

- **Tenant no operacional**: suscripción vencida/cancelada/past_due o con el
  período/trial vencido. El webhook y el consumer cortan todo el procesamiento.
- **Cuota alcanzada**: se bloquea la nueva unidad de un tope estructural (crear cliente,
  publicar agente, invitar colaborador, subir documento) o, con el crédito del mes agotado,
  el turno (D-79, ADR-0208, HU-BILL-18: el crédito es el único tope de consumo).

Un tenant SIN suscripción se considera operacional (grandfathered): el gate
solo bloquea ante evidencia positiva de impago, para no cortar datos legados o
tenants creados fuera de `onboard_tenant`. Todo tenant real recibe su
suscripción `free` al alta (ADR-0045) y por backfill (migración `0034`).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.billing.metrics import count_metric, get_metric
from core.billing.plans import PACK_LIMIT_KEYS, get_plan
from core.billing.service import get_active_subscription
from core.db.models import Tenant
from core.shared.logger import get_logger
from core.tenants import cargar_tenant_activo

if TYPE_CHECKING:
    from core.billing.creditos import DecisionDelTurno

log = get_logger(__name__)

BLOCKING_STATUSES = ("expired", "canceled", "past_due")

class QuotaExceeded(Exception):
    """La acción excede una cuota del plan. Recuperable: el caller decide qué hacer."""

    def __init__(self, metric: str, used: int, limit: int) -> None:
        self.metric = metric
        self.used = used
        self.limit = limit
        super().__init__(f"Cuota agotada: {metric} ({used}/{limit})")


class PlanNoIncluye(Exception):
    """El plan del negocio no incluye un canal o una habilidad (ADR-0208 §9-10). Recuperable: el caller
    responde 409 con `code` (`plan_channel_not_included`, `plan_plugin_not_included`) y `details`, sin
    escribir nada. Las subclases viven en `canales_del_plan` y `habilidades_del_plan`."""

    code = "plan_not_included"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    metric: str
    used: int
    limit: int | None
    reason: str | None = None


def _deadline(sub) -> datetime | None:
    if sub.status == "trialing":
        return sub.trial_ends_at or sub.period_end
    return sub.period_end


async def tenant_operativo(tenant_id: uuid.UUID | None) -> Tenant | None:
    """La fila del tenant si puede procesar mensajes (ciclo de vida + billing), si no `None`. Es la misma lectura
    que `is_tenant_operational`; el consumer la usa para no volver a leer `tenants` en el mismo mensaje."""
    tenant = await cargar_tenant_activo(tenant_id)
    if tenant is None:
        return None
    sub = await get_active_subscription(tenant.id)
    if sub is None:
        log.debug("billing_no_subscription_grandfathered", tenant_id=str(tenant.id))
        return tenant
    if sub.status in BLOCKING_STATUSES:
        return None
    deadline = _deadline(sub)
    if deadline is None or datetime.now(UTC) < deadline:
        return tenant
    return None


async def is_tenant_operational(tenant_id: uuid.UUID | None) -> bool:
    """¿El tenant puede procesar mensajes (ciclo de vida + billing)?"""
    return await tenant_operativo(tenant_id) is not None


async def quota_decision(tenant_id: uuid.UUID, key: str, *, session=None) -> QuotaDecision:
    """¿Le queda cuota al tenant para una nueva unidad de `key`?"""
    metric = get_metric(key)
    if metric is None:
        return QuotaDecision(True, key, 0, None)
    sub = await get_active_subscription(tenant_id, session=session)
    if sub is None:
        return QuotaDecision(True, key, 0, None)  # sin suscripción: no se cobra
    plan = get_plan(sub.plan_id)
    limits = plan.limits if plan else {}
    if key not in limits:
        return QuotaDecision(True, key, 0, None)
    limit = limits[key]
    period_start = sub.period_start if metric.scope == "period" else None
    used = await count_metric(tenant_id, key, period_start=period_start, session=session)
    if limit is None:
        return QuotaDecision(True, key, used, None)
    if used >= limit:
        return QuotaDecision(False, key, used, limit, "cuota_agotada")
    return QuotaDecision(True, key, used, limit)


async def check_metric(tenant_id: uuid.UUID, key: str, *, session=None) -> bool:
    return (await quota_decision(tenant_id, key, session=session)).allowed


async def enforce_metric(tenant_id: uuid.UUID, key: str, *, session=None) -> None:
    """Levanta `QuotaExceeded` si la métrica está agotada. Nada si hay cupo.

    `session`: el caller puede pasar su sesión abierta (dentro de su propio
    `tenant_scope`) para no abrir una sesión anidada — ver `count_metric`.
    """
    decision = await quota_decision(tenant_id, key, session=session)
    if not decision.allowed and decision.limit is not None:
        raise QuotaExceeded(key, decision.used, decision.limit)


# Métricas estructurales de D-35 (ADR-0142): se cuentan en vivo y solo bloquean CRECER. Un
# negocio que ya está por encima del tope (plan bajado, catálogo nuevo) sigue operando igual;
# lo único que se rechaza es la siguiente unidad.
GROWTH_METRICS = ("agentes", "colaboradores", "conexiones", "correos")


async def lock_growth(tenant_id: uuid.UUID, key: str, *, session) -> None:
    """Serializa las altas de una métrica estructural del negocio hasta el fin de la transacción
    de `session` (reentrante). El caller que decide ANTES si la unidad es nueva (p. ej. si el
    agente ya cuenta) lo toma primero, para que esa lectura también quede serializada."""
    from sqlalchemy import text

    if key not in GROWTH_METRICS and key not in PACK_LIMIT_KEYS:
        raise ValueError(f"{key} no es una métrica estructural")
    # El lock, el plan y el conteo corren en la sesión del caller, con la RLS de su scope: con otro `tenant_id` el
    # conteo vería 0 y el lock serializaría a otro negocio (auditor-multitenant, HU-BILL-19).
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise ValueError("lock_growth: tenant_id distinto del tenant_scope vigente")
    await session.execute(
        text("select pg_advisory_xact_lock(hashtext(:m), hashtext(:t))"), {"m": f"billing:{key}", "t": str(tenant_id)}
    )


async def enforce_growth(tenant_id: uuid.UUID, key: str, *, session) -> None:
    """Rechaza (`QuotaExceeded`) una unidad NUEVA de una métrica estructural si el plan está al
    tope. Serializa por negocio y métrica con `pg_advisory_xact_lock` (dos altas concurrentes no
    pasan las dos con un cupo), así que `session` es obligatoria: la del caller, abierta dentro
    del `tenant_scope` del negocio, y el conteo corre en ella después del lock. El lock dura lo
    que la transacción del caller, que es la que inserta la unidad."""

    await lock_growth(tenant_id, key, session=session)
    decision = await quota_decision(tenant_id, key, session=session)
    if not decision.allowed and decision.limit is not None:
        log.info(
            "plan_limit_reached", tenant_id=str(tenant_id), metric=key,
            used=decision.used, limit=decision.limit,
        )
        raise QuotaExceeded(key, decision.used, decision.limit)


async def enforce_pack_growth(
    tenant_id: uuid.UUID,
    key: str,
    contar: Callable[[Any], Awaitable[float]],
    *,
    incremento: float = 1,
    session: Any,
) -> None:
    """Rechaza (`QuotaExceeded`) crecer un tope que cuenta un pack (`PACK_LIMIT_KEYS`, ADR-0145
    §2) si `actual + incremento` supera el límite del plan. El core no consulta las tablas del
    pack: `contar(session)` es del pack y mide en la unidad del tope (documentos, MB).

    Mismo contrato que `enforce_growth`: toma el advisory lock del negocio y la clave ANTES de
    contar, en la `session` del caller (abierta en su `tenant_scope`), y el lock dura hasta el fin
    de esa transacción, que es la que inserta la unidad. Sin suscripción, sin el tope en el plan o
    con tope `None`: no hay límite."""
    if key not in PACK_LIMIT_KEYS:
        raise ValueError(f"{key} no es un tope de pack")
    # El conteo del pack pasa por la RLS del scope: con otro `tenant_id`, el lock y el plan
    # serían de un negocio y el conteo de otro.
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise ValueError("enforce_pack_growth: tenant_id distinto del tenant_scope vigente")
    await lock_growth(tenant_id, key, session=session)
    sub = await get_active_subscription(tenant_id, session=session)
    plan = get_plan(sub.plan_id) if sub is not None else None
    limite = (plan.limits if plan else {}).get(key)
    if limite is None:
        return
    usado = await contar(session)
    if usado + incremento > limite:
        log.info("plan_limit_reached", tenant_id=str(tenant_id), metric=key, used=usado, limit=limite)
        raise QuotaExceeded(key, math.ceil(usado), limite)


async def turn_quota_metric(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> DecisionDelTurno:
    """Decisión del turno con el saldo del negocio: `normal`, `colchon` o `agotado` (ADR-0208 §2-6).

    Lectura sin lock consultivo (`creditos.decidir_turno`). La conversación decide si está abierta
    (colchón) con el episodio releído, así que se llama después de `fijar_release_del_turno`. Con
    `BILLING_BLOQUEAR_POR_CREDITOS` apagado siempre es `normal`."""
    from core.billing.creditos import decidir_turno
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return await decidir_turno(session, tenant_id, conversation_id)


@dataclass(frozen=True)
class SubscriptionOverview:
    plan_id: str | None
    plan_name: str
    status: str
    operational: bool
    trial_ends_at: datetime | None
    period_start: datetime | None
    period_end: datetime | None
    days_left: int | None
    limits: dict[str, int | None]
    # Día ancla del calendario de cobro (ADR-0123): el mes de consumo del crédito lo usa (ADR-0144).
    dia_ancla: int | None = None


async def subscription_overview(tenant_id: uuid.UUID) -> SubscriptionOverview:
    """Estado de la suscripción para el panel (nunca lanza por datos faltantes)."""
    sub = await get_active_subscription(tenant_id)
    if sub is None:
        return SubscriptionOverview(
            plan_id=None, plan_name="—", status="sin_suscripcion", operational=True,
            trial_ends_at=None, period_start=None, period_end=None, days_left=None, limits={},
        )
    plan = get_plan(sub.plan_id)
    deadline = _deadline(sub)
    days_left = None
    if deadline is not None:
        days_left = max(0, (deadline - datetime.now(UTC)).days)
    return SubscriptionOverview(
        plan_id=sub.plan_id,
        plan_name=plan.name if plan else sub.plan_id,
        status=sub.status,
        operational=await is_tenant_operational(tenant_id),
        trial_ends_at=sub.trial_ends_at,
        period_start=sub.period_start,
        period_end=sub.period_end,
        days_left=days_left,
        limits=dict(plan.limits) if plan else {},
        dia_ancla=sub.dia_ancla,
    )


@dataclass(frozen=True)
class PlanSummary:
    """Resumen mínimo del plan para el shell de panel y app (HU-BILL-15).

    No consulta el estado operativo ni el consumo: `GET /admin/me` se llama en cada
    navegación y solo necesita saber si el negocio está en Free y cuánto le queda.
    """

    plan_id: str | None
    plan_name: str
    status: str
    days_left: int | None
    trial_ends_at: datetime | None
    has_plan: bool


async def plan_summary(tenant_id: uuid.UUID | None) -> PlanSummary | None:
    """Plan activo del negocio, sin efectos secundarios ni métricas de consumo."""
    if tenant_id is None:
        return None
    sub = await get_active_subscription(tenant_id)
    if sub is None:
        return PlanSummary(
            plan_id=None,
            plan_name="—",
            status="sin_suscripcion",
            days_left=None,
            trial_ends_at=None,
            has_plan=False,
        )
    plan = get_plan(sub.plan_id)
    deadline = _deadline(sub)
    days_left = max(0, (deadline - datetime.now(UTC)).days) if deadline is not None else None
    return PlanSummary(
        plan_id=sub.plan_id,
        plan_name=plan.name if plan else sub.plan_id,
        status=sub.status,
        days_left=days_left,
        trial_ends_at=sub.trial_ends_at,
        has_plan=True,
    )
