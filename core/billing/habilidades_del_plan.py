"""Habilidades por plan: el `min_plan` de cada pack (D-79, ADR-0208 §10, HU-BILL-19).

Instalar una habilidad en un agente exige que el plan del negocio alcance el `min_plan` del pack
(`core/mcp/types.py`), con el orden del catálogo (`plans.plan_alcanza`: free < basic < premium <
empresarial). Si no alcanza: 409 `plan_plugin_not_included` y no se escribe nada.

Mismo criterio que el resto del enforcement: un negocio sin suscripción o con un plan que el catálogo
no conoce no se bloquea. Un negocio que ya tiene una habilidad por encima de su plan la conserva en el
agente que la tiene, pero no la instala en otro.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from core.billing.enforcement import PlanNoIncluye
from core.billing.plans import get_plan, plan_alcanza
from core.shared.logger import get_logger

log = get_logger(__name__)


class HabilidadNoIncluida(PlanNoIncluye):
    """El plan no alcanza el `min_plan` de una o más habilidades (409 `plan_plugin_not_included`)."""

    code = "plan_plugin_not_included"

    def __init__(self, packs: dict[str, str], plan_id: str | None, plan_name: str) -> None:
        self.packs = dict(packs)
        requerido = max(
            (get_plan(p) for p in packs.values() if get_plan(p) is not None),
            key=lambda plan: plan.sort_order,
            default=None,
        )
        minimo = requerido.name if requerido else ", ".join(sorted(set(packs.values())))
        super().__init__(
            f"Tu plan {plan_name} no incluye esta habilidad: necesita el plan {minimo} o superior. "
            "Mejora tu plan para habilitarla. No se cambió nada.",
            details={"skills": sorted(packs), "min_plan": requerido.id if requerido else None, "plan": plan_id},
        )


def faltantes(plan_id: str | None, min_planes: dict[str, str]) -> dict[str, str]:
    """`{pack: min_plan}` de los packs cuyo `min_plan` no alcanza `plan_id` (puro)."""
    return {pack: minimo for pack, minimo in min_planes.items() if not plan_alcanza(plan_id, minimo)}


def _min_planes(packs: Iterable[str]) -> dict[str, str]:
    from core.plugin_manager import plugin_manager

    salida: dict[str, str] = {}
    for pack in packs:
        modulo = plugin_manager.get(pack)
        if modulo is not None:
            salida[pack] = getattr(modulo, "min_plan", "free") or "free"
    return salida


async def exigir_min_plan(tenant_id: uuid.UUID, packs: Iterable[str], *, session: Any = None) -> None:
    """Levanta `HabilidadNoIncluida` si el plan del negocio no alcanza el `min_plan` de algún pack. No
    escribe nada; quien llama lo hace ANTES de activar, configurar o instalar. Un pack que el despliegue
    no trae se ignora aquí (lo rechaza quien llama con su propio error)."""
    from core.billing.service import get_active_subscription
    from core.shared.tenant import exigir_tenant_scope

    min_planes = _min_planes(packs)
    if not min_planes:
        return
    if session is not None:
        exigir_tenant_scope(tenant_id, "candado del plan")
    sub = await get_active_subscription(tenant_id, session=session)
    plan = get_plan(sub.plan_id) if sub is not None else None
    if plan is None:
        return  # sin suscripción o sin plan conocido: grandfathered
    sin_plan = faltantes(plan.id, min_planes)
    if sin_plan:
        log.info("plan_plugin_not_included", tenant_id=str(tenant_id), packs=sorted(sin_plan), plan=plan.id)
        raise HabilidadNoIncluida(sin_plan, plan.id, plan.name)


__all__ = ["HabilidadNoIncluida", "exigir_min_plan", "faltantes"]
