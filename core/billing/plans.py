"""Catálogo de planes (ADR-0045).

La fuente de verdad de los planes es este módulo (tipado, versionado en git).
`sync_plans()` lo espeja a la tabla de sistema `plans` (sin RLS) para que el
panel y las FKs lo consulten. Mismo patrón que `plugin_manager.sync_registry()`.

`limits` usa las claves de `core.billing.metrics.METRIC_KEYS`. `None` = ilimitado.

Planes v3 por créditos (D-79, SPEC-51, ADR-0208, HU-BILL-18): mismos `plan_id`, precios y
créditos nuevos. El crédito mensual (`monthly_credits`) es el único tope de consumo: los topes de
volumen de v1 (`conversaciones`, audio, OCR, `mensajes`, `costo_llm_usd`) y `plugins` salieron del
registro de métricas. Lo que queda en `limits` son topes estructurales, que solo bloquean un alta
(clientes, agentes, colaboradores y, en el pack de conocimiento, documentos y MB).

`features` (ADR-0208 §9, ADR-0209): `canales` lista con el vocabulario de
`ChannelConnection.channel` todos los canales conversacionales del plan; `nivel_ia`, `byok`,
`acumulable_meses`, `descuento_recarga` (`None` = el plan no compra recargas), `marca_propia`,
`historial_dias` (`None` = ilimitado), `campanas` y `soporte`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.billing.metrics import METRIC_KEYS
from core.shared.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price_cop: int
    limits: dict[str, int | None]
    features: dict[str, Any] = field(default_factory=dict)
    trial_days: int = 0
    interval_days: int = 30
    sort_order: int = 0
    is_public: bool = True
    # Crédito mensual de uso (1 crédito = COP 10, ADR-0144): el único tope de consumo (D-79,
    # ADR-0208). El turno lo consulta (`enforcement.turn_quota_metric`) y las acciones cotizadas
    # lo verifican con `creditos.alcanza`. `None` = sin crédito definido (no se limita).
    monthly_credits: int | None = None
    currency: str = "COP"

    @property
    def is_free(self) -> bool:
        return self.price_cop <= 0


# Topes que cuenta un pack, no el core (ADR-0145 §2): el pack mide lo suyo y los aplica con
# `enforcement.enforce_pack_growth`. `core/billing/metrics.py` no consulta tablas de packs, así
# que estas claves no son métricas del registro ni salen en las barras de consumo.
PACK_LIMIT_KEYS: frozenset[str] = frozenset({"documentos", "conocimiento_mb"})


def _validate_limits(limits: dict[str, int | None]) -> None:
    unknown = set(limits) - METRIC_KEYS - PACK_LIMIT_KEYS
    if unknown:
        raise ValueError(f"Métricas desconocidas en el plan: {sorted(unknown)}")
    for key, value in limits.items():
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValueError(f"Límite inválido para {key}: {value!r}")


# Fuente única del catálogo: `sync_plans()` lo espeja a la tabla `plans` al arrancar (la
# migración `0034` solo sembró la versión inicial; `upsert_plan` sobrescribe precio, límites y
# features por `plan_id`, así que un catálogo nuevo no necesita migración de datos).
PLANS: tuple[Plan, ...] = (
    Plan(
        id="free",
        name="Free",
        price_cop=0,
        trial_days=30,
        sort_order=0,
        monthly_credits=1_500,
        limits={
            "clientes": 100,
            "agentes": 1,
            "colaboradores": 2,
            # Pack de conocimiento (ADR-0145 §2): documentos vivos y MB subidos.
            "documentos": 3,
            "conocimiento_mb": 10,
        },
        features={
            "canales": ["whatsapp", "web"],
            "nivel_ia": "estandar",
            "byok": False,
            "acumulable_meses": 0,
            "descuento_recarga": None,
            "marca_propia": False,
            "historial_dias": 30,
            "campanas": "ninguna",
            "soporte": "comunidad",
        },
    ),
    Plan(
        id="basic",
        name="Basic",
        price_cop=129_000,
        sort_order=1,
        monthly_credits=12_000,
        limits={
            "clientes": 1_000,
            "agentes": 2,
            "colaboradores": 5,
            "documentos": 20,
            "conocimiento_mb": 100,
        },
        features={
            "canales": ["whatsapp", "web", "telegram"],
            "nivel_ia": "estandar",
            "byok": False,
            "acumulable_meses": 0,
            "descuento_recarga": 0,
            "marca_propia": False,
            "historial_dias": 90,
            "campanas": "recordatorios",
            "soporte": "email",
        },
    ),
    Plan(
        id="premium",
        name="Premium",
        price_cop=299_000,
        sort_order=2,
        monthly_credits=40_000,
        limits={
            "clientes": 5_000,
            "agentes": 5,
            "colaboradores": 15,
            "documentos": 100,
            "conocimiento_mb": 500,
        },
        features={
            "canales": ["whatsapp", "web", "telegram", "instagram", "messenger"],
            "nivel_ia": "avanzado",
            "byok": False,
            "acumulable_meses": 1,
            "descuento_recarga": 15,
            "marca_propia": True,
            "historial_dias": 365,
            "campanas": "completas",
            "soporte": "prioritario",
        },
    ),
    Plan(
        id="empresarial",
        name="Empresarial",
        price_cop=799_000,
        sort_order=3,
        monthly_credits=120_000,
        limits={
            "clientes": None,
            "agentes": None,
            "colaboradores": 50,
            "documentos": 500,
            "conocimiento_mb": 2_000,
        },
        features={
            "canales": ["whatsapp", "web", "telegram", "instagram", "messenger"],
            "nivel_ia": "avanzado",
            "byok": True,
            "acumulable_meses": 2,
            "descuento_recarga": 25,
            "marca_propia": True,
            "historial_dias": None,
            "campanas": "completas",
            "soporte": "dedicado",
        },
    ),
)

for _plan in PLANS:
    _validate_limits(_plan.limits)

PLANS_BY_ID: dict[str, Plan] = {plan.id: plan for plan in PLANS}
FREE_PLAN_ID = "free"


def get_plan(plan_id: str) -> Plan | None:
    return PLANS_BY_ID.get(plan_id)


def public_plans() -> list[Plan]:
    return sorted((p for p in PLANS if p.is_public), key=lambda p: p.sort_order)


def plan_limits(plan_id: str) -> dict[str, int | None]:
    plan = get_plan(plan_id)
    return dict(plan.limits) if plan else {}


def _features_espejadas(plan: Plan) -> dict[str, Any]:
    """`features` que se espejan a la tabla: el crédito mensual va ahí (sin migración: la
    tabla `plans` no tiene columna propia y `features` ya es jsonb)."""
    features = dict(plan.features)
    if plan.monthly_credits is not None:
        features["credito_mensual"] = plan.monthly_credits
    return features


async def sync_plans() -> int:
    """Espeja el catálogo de código a la tabla `plans` (upsert idempotente).

    Escribe vía `upsert_plan()` SECURITY DEFINER: `app_runtime` (rol del runtime)
    solo tiene SELECT sobre `plans`, porque el catálogo define precios y límites
    compartidos y no debe ser alterable desde una sesión de tenant.

    No borra planes ausentes del código (un plan retirado se marca a mano para
    no romper FKs de suscripciones históricas). Devuelve cuántos tocó.
    """
    from sqlalchemy import text

    from core.db.session import get_session

    try:
        async with get_session() as session:
            for plan in PLANS:
                await session.execute(
                    text(
                        "select upsert_plan(:id, :name, :price_cop, :currency, "
                        ":interval_days, :trial_days, cast(:limits as jsonb), "
                        "cast(:features as jsonb), :sort_order, :is_public, :version)"
                    ),
                    {
                        "id": plan.id,
                        "name": plan.name,
                        "price_cop": plan.price_cop,
                        "currency": plan.currency,
                        "interval_days": plan.interval_days,
                        "trial_days": plan.trial_days,
                        "limits": __import__("json").dumps(plan.limits),
                        "features": __import__("json").dumps(_features_espejadas(plan)),
                        "sort_order": plan.sort_order,
                        "is_public": plan.is_public,
                        "version": 1,
                    },
                )
            await session.commit()
        return len(PLANS)
    except Exception as exc:  # tabla/función ausente antes de migrar: no tumbar el arranque
        log.warning("plans_sync_failed", error=type(exc).__name__)
        return 0


__all__ = [
    "FREE_PLAN_ID",
    "PLANS",
    "PLANS_BY_ID",
    "Plan",
    "get_plan",
    "plan_limits",
    "public_plans",
    "sync_plans",
]
