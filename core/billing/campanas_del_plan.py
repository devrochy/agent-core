"""Campañas, recordatorios y seguimiento de leads por plan (D-79, SPEC-51, HU-BILL-26).

`features.campanas` del plan tiene tres niveles, en este orden: `ninguna` < `recordatorios` < `completas`.

- **`recordatorios`** (desde Basic): los recordatorios automáticos de citas (`pack_calendar.recordatorios_de_citas`).
- **`completas`** (Premium y Empresarial): el envío de una plantilla a un segmento de Clientes (HU-CRM-06) y el
  seguimiento automático de leads (`core.leads_follow_up`).

Una acción del panel que el plan no incluye responde 409 `plan_feature_not_included` con
`details = {feature: "campanas", plan}` y no escribe nada. Un cron no envía y deja el motivo (ver cada cron).

Mismo criterio que el resto del enforcement: un negocio sin suscripción o con un plan que el catálogo no conoce no
se bloquea. Lo que no salió no se reenvía solo al subir de plan (mismo criterio que la pausa, D-76).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from core.billing.enforcement import PlanNoIncluye
from core.billing.plans import get_plan
from core.shared.logger import get_logger

log = get_logger(__name__)

FEATURE = "campanas"
NIVELES: tuple[str, ...] = ("ninguna", "recordatorios", "completas")
RECORDATORIOS = "recordatorios"
COMPLETAS = "completas"
# Motivo que queda en lo que un cron no envió (`messages.error_reason`, resumen del cron, logs).
MOTIVO = "plan_feature_not_included"

TipoDeCampana = Literal["recordatorios", "completas"]

_QUE_ES = {
    RECORDATORIOS: "los recordatorios automáticos",
    COMPLETAS: "las campañas y el seguimiento automático de leads",
}
_DESDE = {RECORDATORIOS: "Basic", COMPLETAS: "Premium"}


class CampanaNoIncluida(PlanNoIncluye):
    """El plan no incluye ese nivel de campañas (409 `plan_feature_not_included`)."""

    code = MOTIVO

    def __init__(self, tipo: str, plan_id: str | None, plan_name: str) -> None:
        self.tipo = tipo
        super().__init__(
            f"Tu plan {plan_name} no incluye {_QUE_ES.get(tipo, 'esta función')}: están desde el plan "
            f"{_DESDE.get(tipo, 'Premium')}. Mejora tu plan para usarlas. No se envió nada.",
            details={"feature": FEATURE, "plan": plan_id},
        )


def _nivel(valor: Any) -> int:
    return NIVELES.index(valor) if valor in NIVELES else 0


def nivel_de_campanas(plan_id: str | None) -> str:
    """El nivel de `features.campanas` del plan (puro). Un plan vacío, desconocido o sin la clave es `ninguna`."""
    plan = get_plan(plan_id) if plan_id else None
    valor = (plan.features.get(FEATURE) if plan else None) or "ninguna"
    return valor if valor in NIVELES else "ninguna"


def plan_incluye_campanas(plan_id: str | None, tipo: str) -> bool:
    """¿El plan alcanza el nivel `tipo` (`recordatorios` | `completas`)? Pura; la tolerancia de los negocios sin
    suscripción la aplica `campana_permitida`."""
    if tipo not in NIVELES or tipo == "ninguna":
        raise ValueError(f"tipo de campaña desconocido: {tipo!r}")
    return _nivel(nivel_de_campanas(plan_id)) >= _nivel(tipo)


async def _plan_del_negocio(tenant_id: uuid.UUID, session: Any = None) -> tuple[str | None, Any]:
    from core.billing.service import get_active_subscription
    from core.shared.tenant import exigir_tenant_scope

    if session is not None:
        # Sin sesión, `get_active_subscription` abre su propio scope; con la del caller, tiene que ser la del negocio.
        exigir_tenant_scope(tenant_id, "candado del plan")
    sub = await get_active_subscription(tenant_id, session=session)
    if sub is None:
        return None, None
    return sub.plan_id, get_plan(sub.plan_id)


async def plan_que_frena(tenant_id: uuid.UUID, tipo: TipoDeCampana, *, session: Any = None) -> str | None:
    """El `plan_id` del negocio si su plan NO incluye `tipo`; None si lo incluye o si no se bloquea (sin
    suscripción o con un plan que el catálogo no conoce). Para los crons, que dejan el plan en el registro."""
    plan_id, plan = await _plan_del_negocio(tenant_id, session)
    if plan is None or plan_incluye_campanas(plan_id, tipo):
        return None
    return plan_id


async def campana_permitida(tenant_id: uuid.UUID, tipo: TipoDeCampana, *, session: Any = None) -> bool:
    """¿El negocio puede usar `tipo`? Sin suscripción o sin plan conocido, sí."""
    return await plan_que_frena(tenant_id, tipo, session=session) is None


async def exigir_campana(tenant_id: uuid.UUID, tipo: TipoDeCampana, *, session: Any = None) -> None:
    """Levanta `CampanaNoIncluida` si el plan del negocio no incluye `tipo`. No escribe nada: quien llama lo hace
    ANTES de guardar o enviar."""
    plan_id, plan = await _plan_del_negocio(tenant_id, session)
    if plan is None or plan_incluye_campanas(plan_id, tipo):
        return
    log.info(MOTIVO, tenant_id=str(tenant_id), feature=FEATURE, tipo=tipo, plan=plan_id)
    raise CampanaNoIncluida(tipo, plan_id, plan.name)


__all__ = [
    "COMPLETAS",
    "FEATURE",
    "MOTIVO",
    "NIVELES",
    "RECORDATORIOS",
    "CampanaNoIncluida",
    "campana_permitida",
    "exigir_campana",
    "nivel_de_campanas",
    "plan_incluye_campanas",
    "plan_que_frena",
]
