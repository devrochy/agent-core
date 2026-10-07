"""SubscriptionsModule: suscripciones de cliente final (ADR-0083, planes como productos).

El plan es un producto de commerce (variante con `billing_period`). Cuando un
pedido con ese producto se paga, commerce entrega un snapshot del plan y este
pack crea/activa la `Subscription` del `Customer` (idempotente por cliente) a
través del registry del core — sin importar commerce (invariante nº5). También
escucha `payment.confirmed` para marcar los cargos pagados y expone una API
admin para crear suscripciones manualmente (mismo snapshot).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import select

import pack_subscriptions.jobs  # noqa: F401  # registra el @register_cron (ADR-0083/#156)
from core.db.session import get_session
from core.mcp.types import BaseModule, GuardrailResult
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope
from core.subscriptions import subscription_registry
from pack_subscriptions.admin_api import router as subscriptions_admin_api_router
from pack_subscriptions.capability import (
    SubscriptionCapabilityImpl,
    crear_suscripcion_para,
)
from pack_subscriptions.config import SubscriptionConfig
from pack_subscriptions.models import Subscription, SubscriptionCharge

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"

_PAUSAR = re.compile(r"suscripci[oó]n\s+(?:qued\w*\s+)?pausad\w*|paus\w+\s+(?:tu\s+)?suscripci", re.IGNORECASE)
_CANCELAR = re.compile(r"suscripci[oó]n\s+(?:qued\w*\s+)?(?:cancel\w*|anulad\w*)|cancel\w+\s+(?:tu\s+)?suscripci", re.IGNORECASE)


# `crear_suscripcion_para` vive en capability.py (proveedor del contrato v1,
# ADR-0106); se re-exporta para la API admin y los tests que la importan de acá.
__all__ = ["crear_suscripcion_para"]


class SubscriptionsModule(BaseModule):
    id = PLUGIN_ID
    name = "Suscripciones"
    version = "0.1.0"
    description = "Suscripción de cliente final activada por pago de un plan (ADR-0083)"
    display_name = "Suscripciones"
    category = "Cobros"
    min_plan = "basic"
    required_connections: ClassVar[list[str]] = ["pack_payments"]
    action_summary: ClassVar[list[str]] = ["Cobrar el período", "Enviar link", "Gestionar estado"]
    setup_guide = "Define tus planes con periodicidad y activa los cobros recurrentes."
    requires_config = True
    config_schema = SubscriptionConfig
    declared_skills: list[type] = []  # noqa: RUF012 -- las tools sub_* viven en el core (ADR-0106)
    admin_menu: list[dict[str, str]] = [  # noqa: RUF012
        {"label": "Suscripciones", "path": "/subscriptions/lista", "section": "Operación", "section_key": "pack_subscriptions:suscripciones"},
    ]

    def on_activate(self, host: MCPHost) -> None:
        host.register_guardrail("subs_completion_check", self._completion_guardrail, priority=6)
        subscription_registry.register(PLUGIN_ID, SubscriptionCapabilityImpl())
        host.subscribe("payment.confirmed", self._on_payment_confirmed)

    def on_deactivate(self) -> None:
        subscription_registry.unregister(PLUGIN_ID)

    def admin_api_routes(self) -> list[Any]:
        """API admin de suscripciones (ADR-0079), montada en `/api/v1`."""
        return [subscriptions_admin_api_router]

    async def _on_payment_confirmed(self, reference: str, provider: str, provider_reference: str, amount: float) -> None:
        """Marca pagado el cargo del período cuyo `referencia` es `sub-<charge.id>`."""
        if not reference or not reference.startswith("sub-"):
            return
        try:
            charge_id = uuid.UUID(reference[4:])
        except (ValueError, TypeError):
            return
        tenant_id = get_current_tenant_id()
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                cargo = (
                    await session.execute(
                        select(SubscriptionCharge).where(
                            SubscriptionCharge.tenant_id == tenant_id,
                            SubscriptionCharge.id == charge_id,
                        )
                    )
                ).scalar_one_or_none()
                if cargo is None:
                    return
                cargo.estado = "pagado"
                sub = await session.get(Subscription, cargo.subscription_id)
                if sub is not None and sub.estado == "trialing" and sub.trial_termina and sub.trial_termina <= datetime.now(UTC):
                    sub.estado = "activa"
        log.info("subs_charge_pagado", reference=reference)

    async def _completion_guardrail(
        self, response_text: str, tool_results: list[dict[str, Any]]
    ) -> GuardrailResult:
        """Bloquea afirmar cambios de suscripción sin la tool correspondiente (ADR-0083)."""
        called = {r.get("_tool_name") for r in (tool_results or [])}
        checks = [
            (_PAUSAR, {"sub_pausar"}),
            (_CANCELAR, {"sub_cancelar"}),
        ]
        for pattern, tools in checks:
            if pattern.search(response_text or "") and not (called & tools):
                return GuardrailResult(
                    passed=False,
                    reason="Afirmación de suscripción sin la tool correspondiente en el turno.",
                    retry_instruction=(
                        f"Afirmaste algo sobre la suscripción pero no llamaste la herramienta "
                        f"({', '.join(sorted(tools))}). Llamala ahora, o si no se pudo, decilo "
                        "explícitamente en vez de prometerlo."
                    ),
                )
        return GuardrailResult(passed=True)