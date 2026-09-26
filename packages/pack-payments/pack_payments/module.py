"""PaymentsModule: BaseModule con webhook público propio (confirmación de Wompi).

Emite eventos de dominio (`ON_PAYMENT_CONFIRMED`/`ON_PAYMENT_REJECTED`) para que
otros packs (p. ej. cobranza) reaccionen sin que este pack los importe --
invariante nº5. Los topics viven ahora en el **core** (ADR-0067).

Registra su `PaymentCapability` (ADR-0067) para que otros packs creen links de
pago sin importarlo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import BaseModule
from core.payments import payment_registry
from core.shared.events import ON_PAYMENT_CONFIRMED, ON_PAYMENT_REJECTED
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope
from pack_payments.admin import router as admin_config_router
from pack_payments.config import PaymentsConfig
from pack_payments.models import PaymentTransaction
from pack_payments.providers.base import WebhookEvent
from pack_payments.skills import PaymentSkill, build_provider, tenant_config

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

PLUGIN_ID = "pack_payments"

admin_router = APIRouter(prefix="/webhooks/payments/wompi", tags=[PLUGIN_ID])


@admin_router.post("/{slug}")
async def wompi_webhook(request: Request, slug: str) -> dict[str, Any]:
    """Confirmación asíncrona de pago. Público (cae en el allowlist de
    `/webhooks` del middleware de tenant, core/main.py) -- Wompi no manda
    cookie de sesión admin, se autentica con el checksum del payload.
    """
    from core.tenants import is_tenant_active, resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        log.warning("wompi_webhook_tenant_not_found", slug=slug)
        return {"status": "ignored"}
    if not await is_tenant_active(tenant_id):
        log.warning("wompi_webhook_tenant_not_active", tenant_id=str(tenant_id))
        return {"status": "ignored"}

    payload = await request.json()

    async with tenant_scope(tenant_id):
        try:
            config = await tenant_config()
        except PluginError:
            log.warning("wompi_webhook_not_configured", tenant_id=str(tenant_id))
            return {"status": "ignored"}

        provider = build_provider(config)
        if not provider.verify_webhook_signature(payload):
            log.warning("wompi_webhook_invalid_signature", tenant_id=str(tenant_id))
            return {"status": "ignored"}

        event = provider.parse_webhook_event(payload)
        transaction, status_changed = await _apply_webhook_event(event, payload)
        # Wompi reintenta la entrega del webhook por red (no es un duplicado de otro
        # tenant, es el mismo evento repetido) -- emitir en cada reintento duplicaría
        # el efecto en cualquier suscriptor futuro no idempotente (ej. notificar al
        # cliente, descontar inventario). Solo se emite en la transición real de estado.
        if transaction is not None and status_changed and event.status in ("aprobado", "rechazado"):
            from core.shared.events import event_bus

            topic = ON_PAYMENT_CONFIRMED if event.status == "aprobado" else ON_PAYMENT_REJECTED
            await event_bus.emit(
                topic,
                reference=transaction.reference,
                provider=provider.provider_name,
                provider_reference=transaction.provider_reference,
                amount=float(transaction.amount),
            )

    return {"status": "ok"}


async def _apply_webhook_event(
    event: WebhookEvent, raw_payload: dict[str, Any]
) -> tuple[PaymentTransaction | None, bool]:
    async with get_session() as session:
        transaction = (
            await session.execute(
                select(PaymentTransaction).where(
                    PaymentTransaction.tenant_id == get_current_tenant_id(),
                    PaymentTransaction.provider_reference == event.provider_reference,
                )
            )
        ).scalar_one_or_none()
        if transaction is None:
            log.warning("wompi_webhook_no_matching_transaction", provider_reference=event.provider_reference)
            return None, False
        status_changed = transaction.status != event.status
        transaction.status = event.status
        transaction.last_webhook_payload = raw_payload
        await session.flush()
        await session.refresh(transaction)
        return transaction, status_changed


class PaymentsModule(BaseModule):
    id = PLUGIN_ID
    name = "Pagos (Wompi)"
    version = "0.1.0"
    description = "Links de pago en línea con confirmación automática por webhook"
    requires_config = True
    config_schema = PaymentsConfig
    admin_router = admin_router
    declared_skills: list[type] = [PaymentSkill]  # noqa: RUF012

    def on_activate(self, host: MCPHost) -> None:
        config = self.config
        if not isinstance(config, PaymentsConfig):
            from core.shared.exceptions import PluginConfigError

            raise PluginConfigError(PLUGIN_ID, "Config inválida o ausente")
        host.register_skill(PaymentSkill())
        from pack_payments.payments import WompiPaymentCapability

        payment_registry.register(PLUGIN_ID, WompiPaymentCapability())

    def extra_admin_routers(self) -> list[APIRouter]:
        """Router de la pestaña "Pagos" de Configuración (ADR-0049)."""
        return [admin_config_router]
