"""Catálogo de planes (ADR-0045).

La fuente de verdad de los planes es este módulo (tipado, versionado en git).
`sync_plans()` lo espeja a la tabla de sistema `plans` (sin RLS) para que el
panel y las FKs lo consulten. Mismo patrón que `plugin_manager.sync_registry()`.

`limits` usa las claves de `core.billing.metrics.METRIC_KEYS`. `None` = ilimitado.

Planes v2 por agentes (D-35, HU-BILL-05, ADR-0142): las cifras de `agentes`, `colaboradores`
y `monthly_credits` salen de `modelo-de-precios.md §5` (docs). «Habilidades activas»
(`plugins`) y «Asistentes por negocio» ya no son topes: `plugins` sale de `limits` (la métrica
sigue definida para lecturas). Las métricas de volumen (`conversaciones`, `mensajes`, audio,
OCR, `costo_llm_usd`) se quedan hasta que el crédito tenga ledger y enforcement
(HU-BILL-04/06): retirarlas antes dejaría el costo sin tope.
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
    # Crédito mensual de uso (D-35; 1 crédito = COP 10). Informativo hasta HU-BILL-04/06: el
    # core todavía no mide consumo en créditos. `None` = sin crédito definido.
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
# migración `0034` solo sembró la versión inicial).
PLANS: tuple[Plan, ...] = (
    Plan(
        id="free",
        name="Free",
        price_cop=0,
        trial_days=30,
        sort_order=0,
        monthly_credits=2_500,
        limits={
            "clientes": 100,
            "conversaciones": 500,
            "conversaciones_audio": 50,
            "conversaciones_ocr": 20,
            "agentes": 1,
            "colaboradores": 2,
            "mensajes": 2000,
            "costo_llm_usd": 5,
            # Pack de conocimiento (ADR-0145 §2): documentos vivos y MB subidos.
            "documentos": 3,
            "conocimiento_mb": 10,
        },
        features={"soporte": "comunidad", "canales": ["whatsapp"]},
    ),
    Plan(
        id="basic",
        name="Basic",
        price_cop=200_000,
        sort_order=1,
        monthly_credits=20_000,
        limits={
            "clientes": 500,
            "conversaciones": 3000,
            "conversaciones_audio": 300,
            "conversaciones_ocr": 150,
            "agentes": 2,
            "colaboradores": 5,
            "mensajes": 15000,
            "costo_llm_usd": 50,
            # Pack de conocimiento (ADR-0145 §2): documentos vivos y MB subidos.
            "documentos": 20,
            "conocimiento_mb": 100,
        },
        features={"soporte": "email", "canales": ["whatsapp", "messenger", "instagram"]},
    ),
    Plan(
        id="premium",
        name="Premium",
        price_cop=600_000,
        sort_order=2,
        monthly_credits=75_000,
        limits={
            "clientes": 2000,
            "conversaciones": 15000,
            "conversaciones_audio": 1500,
            "conversaciones_ocr": 800,
            "agentes": 5,
            "colaboradores": 15,
            "mensajes": 80000,
            "costo_llm_usd": 200,
            # Pack de conocimiento (ADR-0145 §2): documentos vivos y MB subidos.
            "documentos": 100,
            "conocimiento_mb": 500,
        },
        features={"soporte": "prioritario", "canales": ["whatsapp", "messenger", "instagram"]},
    ),
    Plan(
        id="empresarial",
        name="Empresarial",
        price_cop=1_800_000,
        sort_order=3,
        monthly_credits=240_000,
        limits={
            "clientes": None,
            "conversaciones": None,
            "conversaciones_audio": None,
            "conversaciones_ocr": None,
            "agentes": None,
            "colaboradores": 50,
            "mensajes": None,
            "costo_llm_usd": None,
            # Pack de conocimiento (ADR-0145 §2): documentos vivos y MB subidos.
            "documentos": 500,
            "conocimiento_mb": 2_000,
        },
        features={"soporte": "dedicado", "canales": ["whatsapp", "messenger", "instagram", "web"]},
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
