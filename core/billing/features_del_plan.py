"""Marca propia, historial y exportación por plan (D-79, SPEC-51, HU-BILL-25).

Tres features del catálogo (`core/billing/plans.py`) que no gastan créditos:

- **`marca_propia`**: el chat web del negocio no muestra «SolversHub». La ruta pública del widget
  (`GET /public/web-chat/{key}/config`) lo expone como `marca_propia`.
- **`historial_dias`**: cuántos días hacia atrás ve el panel. **Solo se oculta en el panel**
  (decisión del dueño, 2026-11-22): no se borra, no se archiva y no hay cron. Las rutas del panel que
  listan conversaciones o mensajes filtran desde `desde_del_historial`; al subir de plan se ve todo al
  instante. La memoria del agente, el turno, los crons y las rutas de plataforma NO usan este módulo.
- **`exportar`**: el CSV de conversaciones (`GET /admin/conversations/export.csv`). Sin la feature,
  409 `plan_feature_not_included` con `details = {feature, plan}`.

Mismo criterio que `canales_del_plan`: un negocio sin suscripción o con un plan que el catálogo no
conoce no se limita (sin filtro y con exportación), salvo la marca, que sigue visible (quitarla es lo
que se paga). Las funciones puras reciben el `plan_id`; las async leen la suscripción del negocio.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from core.billing.enforcement import PlanNoIncluye
from core.billing.plans import Plan, get_plan
from core.shared.logger import get_logger

log = get_logger(__name__)

FEATURE_EXPORTAR = "exportar"
FEATURE_HISTORIAL = "historial"


class FeatureNoIncluida(PlanNoIncluye):
    """El plan no incluye la feature (409 `plan_feature_not_included`, `details = {feature, plan}`)."""

    code = "plan_feature_not_included"

    def __init__(self, feature: str, plan_id: str | None, message: str, **extra: Any) -> None:
        self.feature = feature
        super().__init__(message, details={"feature": feature, "plan": plan_id, **extra})


# ------------------------------------------------------------------ puras (por plan)
def marca_propia(plan_id: str | None) -> bool:
    """¿El plan quita «SolversHub» del chat web? Un plan vacío o desconocido, no."""
    plan = get_plan(plan_id) if plan_id else None
    return bool(plan and plan.features.get("marca_propia"))


def historial_dias(plan_id: str | None) -> int | None:
    """Días de historial que ve el panel; `None` = sin límite (también un plan vacío o desconocido)."""
    plan = get_plan(plan_id) if plan_id else None
    if plan is None:
        return None
    dias = plan.features.get("historial_dias")
    return int(dias) if isinstance(dias, int) and dias > 0 else None


def plan_exporta(plan_id: str | None) -> bool:
    """¿El plan incluye la exportación? Un plan vacío o desconocido no se limita."""
    plan = get_plan(plan_id) if plan_id else None
    return plan is None or bool(plan.features.get(FEATURE_EXPORTAR))


def desde_para(dias: int | None, *, ahora: datetime | None = None) -> datetime | None:
    """El corte del historial para `dias` (`None` = sin corte)."""
    if dias is None:
        return None
    return (ahora or datetime.now(UTC)) - timedelta(days=dias)


# ------------------------------------------------------------------ async (por negocio)
async def _plan_del_negocio(tenant_id: uuid.UUID, session: Any = None) -> tuple[str | None, Plan | None]:
    from core.billing.service import get_active_subscription

    sub = await get_active_subscription(tenant_id, session=session)
    if sub is None:
        return None, None
    return sub.plan_id, get_plan(sub.plan_id)


async def marca_propia_del_negocio(tenant_id: uuid.UUID, *, session: Any = None) -> bool:
    plan_id, _ = await _plan_del_negocio(tenant_id, session)
    return marca_propia(plan_id)


async def desde_del_historial(
    tenant_id: uuid.UUID, *, session: Any = None, ahora: datetime | None = None
) -> datetime | None:
    """Desde cuándo ve el panel conversaciones y mensajes del negocio, según el plan vigente.

    `None` = sin filtro (sin suscripción, plan desconocido o `historial_dias = None`). Se calcula en
    cada request: al subir de plan el corte se corre al instante. Solo para rutas del panel."""
    plan_id, _ = await _plan_del_negocio(tenant_id, session)
    return desde_para(historial_dias(plan_id), ahora=ahora)


async def historial_del_negocio(tenant_id: uuid.UUID, *, session: Any = None) -> tuple[str | None, int | None]:
    """`(plan_id, historial_dias)` del negocio, para explicar el corte en un aviso."""
    plan_id, _ = await _plan_del_negocio(tenant_id, session)
    return plan_id, historial_dias(plan_id)


async def exigir_exportar(tenant_id: uuid.UUID, *, session: Any = None) -> None:
    """Levanta `FeatureNoIncluida` si el plan del negocio no incluye la exportación."""
    plan_id, plan = await _plan_del_negocio(tenant_id, session)
    if plan is None or plan_exporta(plan_id):
        return
    log.info("plan_feature_not_included", tenant_id=str(tenant_id), feature=FEATURE_EXPORTAR, plan=plan_id)
    raise FeatureNoIncluida(
        FEATURE_EXPORTAR,
        plan_id,
        f"Tu plan {plan.name} no incluye exportar conversaciones. Mejora a Premium para descargarlas.",
    )


def conversacion_fuera_del_historial(plan_id: str | None, dias: int | None) -> FeatureNoIncluida:
    """El aviso de una conversación que el plan ya no deja ver (409, `feature = historial`)."""
    plan = get_plan(plan_id) if plan_id else None
    nombre = plan.name if plan else "actual"
    return FeatureNoIncluida(
        FEATURE_HISTORIAL,
        plan_id,
        f"Esta conversación no tiene actividad en los últimos {dias} días que muestra tu plan {nombre}. "
        "Mejora tu plan para verla: no se borró nada.",
        historial_dias=dias,
    )


__all__ = [
    "FEATURE_EXPORTAR",
    "FEATURE_HISTORIAL",
    "FeatureNoIncluida",
    "conversacion_fuera_del_historial",
    "desde_del_historial",
    "desde_para",
    "exigir_exportar",
    "historial_del_negocio",
    "historial_dias",
    "marca_propia",
    "marca_propia_del_negocio",
    "plan_exporta",
]
