"""PaymentsModule: BaseModule con webhook público propio (confirmación de Wompi).

Escribe eventos de dominio (`ON_PAYMENT_CONFIRMED`/`ON_PAYMENT_REJECTED`, outbox ADR-0107) para que
otros packs (p. ej. cobranza) reaccionen sin que este pack los importe --
invariante nº5. Los topics viven ahora en el **core** (ADR-0067).

Registra su `PaymentCapability` (ADR-0067) para que otros packs creen links de
pago sin importarlo.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar

from fastapi import APIRouter, Request
from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import BaseModule
from core.payments import payment_registry
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope
from pack_payments.admin import router as admin_config_router
from pack_payments.config import PaymentsConfig
from pack_payments.models import PaymentTransaction
from pack_payments.proveedor import build_provider, tenant_config
from pack_payments.providers import wompi
from pack_payments.providers.base import WebhookEvent

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
        resultado = await procesar_evento_de_wompi(payload)
    return {"status": "ignored" if resultado in (SIN_CONFIG, FIRMA_INVALIDA) else "ok"}


SIN_CONFIG = "sin_config"
FIRMA_INVALIDA = "firma_invalida"


async def procesar_evento_de_wompi(payload: dict[str, Any]) -> str:
    """Verifica y aplica un evento de Wompi en el negocio actual (`tenant_scope`).

    Lo usan el webhook propio del pack y, si la cuenta de Wompi es la misma que la de la
    plataforma, el webhook de la plataforma, que reenvía lo que no es suyo (una sola URL de
    eventos por cuenta: riesgo R-05, HU-SH-04, ADR-0123). La firma se verifica siempre con el
    secreto de eventos de ESTE negocio. Devuelve `aplicado`, `sin_transaccion`, `sin_config`
    o `firma_invalida`.
    """
    tenant_id = get_current_tenant_id()
    try:
        config = await tenant_config()
    except PluginError:
        log.warning("wompi_webhook_not_configured", tenant_id=str(tenant_id))
        return SIN_CONFIG

    provider = build_provider(config)
    if not provider.verify_webhook_signature(payload):
        log.warning("wompi_webhook_invalid_signature", tenant_id=str(tenant_id))
        return FIRMA_INVALIDA

    event = provider.parse_webhook_event(payload)
    # El estado del pago y su evento de dominio se escriben en la MISMA
    # transacción (outbox, ADR-0107): si la transacción se revierte, el
    # evento no existe. Wompi reintenta la entrega por red (mismo evento);
    # la escritura es idempotente por `dedupe_key`, así que un reenvío no
    # duplica el efecto.
    transaction = await _apply_webhook_event(event, payload, provider.provider_name)
    return "aplicado" if transaction is not None else "sin_transaccion"


# Estado de una transacción cuyo pago aprobado llegó con un monto distinto al
# del link (o sin monto firmado): no se confirma y queda para revisión manual.
MONTO_DISTINTO = "monto_distinto"


def _monto_coincide(pagado: float | None, esperado: float | Decimal | None) -> bool:
    """El monto que el proveedor firmó coincide con el que pedimos al crear el link.

    `pagado=None` (el proveedor no firmó el monto) nunca coincide: confirmar un
    pago sin saber cuánto se pagó es confiar en que la pasarela respetó el monto
    fijo del link.
    """
    if pagado is None or esperado is None:
        return False
    return abs(Decimal(str(pagado)) - Decimal(str(esperado))) <= Decimal("0.01")


async def _apply_webhook_event(
    event: WebhookEvent, raw_payload: dict[str, Any], provider_name: str
) -> PaymentTransaction | None:
    alerta: dict[str, Any] | None = None
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
            return None

        if (
            event.status == "aprobado"
            and transaction.status != "aprobado"
            and not _monto_coincide(event.amount, transaction.amount)
        ):
            # Un pago aprobado por otro monto no confirma nada: se guarda el
            # payload, la transacción queda en revisión y se avisa a los dueños
            # (una sola vez: los reintentos de Wompi no repiten la alerta). Una
            # transacción ya confirmada no se revierte por un reenvío.
            log.error(
                "wompi_webhook_monto_distinto",
                reference=transaction.reference,
                esperado=str(transaction.amount),
                recibido=str(event.amount),
            )
            if transaction.status != MONTO_DISTINTO:
                alerta = {
                    "transaction_id": str(transaction.id),
                    "reference": transaction.reference,
                    "provider_reference": transaction.provider_reference,
                    "esperado": str(transaction.amount),
                    "recibido": str(event.amount) if event.amount is not None else None,
                }
            transaction.status = MONTO_DISTINTO
            transaction.last_webhook_payload = raw_payload
            await session.flush()
        elif transaction.status == MONTO_DISTINTO and event.status != "aprobado":
            # Ya hay plata en revisión manual: un evento posterior (rechazo de otro
            # intento, pendiente) no la saca de ahí ni emite nada.
            transaction.last_webhook_payload = raw_payload
            await session.flush()
        else:
            status_changed = transaction.status != event.status
            transaction.status = event.status
            transaction.last_webhook_payload = raw_payload
            await session.flush()
            await session.refresh(transaction)

            # Solo se emite en la transición real de estado (como antes); el outbox
            # lo escribe en esta misma transacción, con el tenant explícito.
            if status_changed and event.status in ("aprobado", "rechazado"):
                from core.events.outbox import emit_domain
                from core.shared.events import ON_PAYMENT_CONFIRMED, ON_PAYMENT_REJECTED

                topic = ON_PAYMENT_CONFIRMED if event.status == "aprobado" else ON_PAYMENT_REJECTED
                # Confirmado: el monto que firmó el proveedor (ya validado contra el
                # del link). Rechazado: el del link si el proveedor no lo firmó.
                amount = event.amount if event.amount is not None else float(transaction.amount)
                await emit_domain(
                    session,
                    tenant_id=get_current_tenant_id(),
                    topic=topic,
                    dedupe_key=f"payment:{transaction.provider_reference}:{event.status}",
                    payload={
                        "reference": transaction.reference,
                        "provider": provider_name,
                        "provider_reference": transaction.provider_reference,
                        "amount": amount,
                    },
                )

    # Fuera de la transacción: la alerta solo sale si el cambio de estado se guardó.
    if alerta is not None:
        await _alertar_monto_distinto(alerta)
    return transaction


async def _alertar_monto_distinto(alerta: dict[str, Any]) -> None:
    """Pago aprobado con un monto distinto al del link: auditoría + push a los dueños."""
    from core.shared.audit import record_audit

    await record_audit(
        actor=f"plugin:{PLUGIN_ID}",
        action="payments_pago_monto_distinto",
        output_data=alerta,
    )
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return
    recibido = alerta["recibido"] if alerta["recibido"] is not None else "un monto sin verificar"
    from core.notifications.service import notify_standalone
    from core.notifications.spec import NotificationSpec

    await notify_standalone(
        tenant_id,
        NotificationSpec(
            event_key="payments.pago_monto_distinto",
            category="cobros",
            severity="warning",
            title="Pago en línea con monto distinto",
            body=(
                f"Llegó un pago de {recibido} para {alerta['reference']}, pero el cobro era de "
                f"{alerta['esperado']}. No se confirmó: revísalo."
            ),
            dedupe_key=f"payments_monto_distinto:{alerta['transaction_id']}",
            section="pack_payments",
            data={"kind": "payments_pago_monto_distinto", "transaction_id": alerta["transaction_id"]},
            target={"entity": "pago", "id": str(alerta["transaction_id"]), "section": "pack_payments"},
        ),
    )


class PaymentsModule(BaseModule):
    id = PLUGIN_ID
    name = "Pagos (Wompi)"
    version = "0.1.0"
    description = "Links de pago en línea con confirmación automática por webhook"
    display_name = "Pagos en línea"
    category = "Cobros"
    min_plan = "basic"
    action_summary: ClassVar[list[str]] = ["Generar links de pago", "Confirmar pagos por webhook"]
    setup_guide = "Configura tus llaves de Wompi (sandbox o producción)."
    requires_config = True
    config_schema = PaymentsConfig
    # Salida a red de la capacidad y del panel (ADR-0052): solo la API de Wompi.
    network_allowlist: ClassVar[tuple[str, ...]] = wompi.NETWORK
    admin_router = admin_router

    def on_activate(self, host: MCPHost) -> None:
        config = self.config
        if not isinstance(config, PaymentsConfig):
            from core.shared.exceptions import PluginConfigError

            raise PluginConfigError(PLUGIN_ID, "Config inválida o ausente")
        # Sin tools: el link de un pedido lo pide pack-commerce con `confirmar_pago_pedido`
        # (ADR-0087). `generar_link_pago`, con referencia y monto del LLM, se retiró.
        from pack_payments.payments import WompiPaymentCapability

        payment_registry.register(PLUGIN_ID, WompiPaymentCapability())

    def extra_admin_routers(self) -> list[APIRouter]:
        """Router de la pestaña "Pagos" de Configuración (ADR-0049)."""
        return [admin_config_router]
