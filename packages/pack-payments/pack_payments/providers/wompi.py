"""Proveedor Wompi (pasarela de pagos, Colombia). Sin SDK: httpx nativo.

Verificación de firma de webhook: confirmada contra la documentación
pública de Wompi (docs.wompi.co/docs/colombia/eventos) — concatenar los
valores de `signature.properties` (en el orden dado, sin separador) +
`timestamp` + el `events_secret` del tenant, SHA256, comparar contra
`signature.checksum`. Este algoritmo SÍ está verificado.

El body de creación de un link de pago (`POST /v1/payment_links`) se
verificó contra el sandbox REAL de Wompi (encontrado en vivo, tenant
yopoldo, 2026-09-11: un 422 real por `collect_shipping` faltante) y contra
la implementación ya probada de `agent-sale`
(`src/payments/wompiClient.ts`), que documenta además un mínimo real de
Wompi para el monto (150.000 COP) no explícito en su guía pública —
descubierto ahí también por QA contra el sandbox real, no por
documentación.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.shared.logger import get_logger
from pack_payments.providers.base import (
    BasePaymentProvider,
    PaymentLinkResult,
    PaymentRequestRejected,
    WebhookEvent,
)

log = get_logger(__name__)

PRODUCTION_BASE_URL = "https://production.wompi.co/v1"
SANDBOX_BASE_URL = "https://sandbox.wompi.co/v1"

# Mínimo real de Wompi para la base de una transacción en COP -- no está en
# su guía pública de Links de pago, lo confirmó agent-sale por QA contra el
# sandbox real (mismo 422 que si no se chequea acá antes de llamar).
MIN_AMOUNT_COP = 150_000

# Wompi usa su propio vocabulario de estado de transacción; lo normalizamos
# al de este pack para que ningún pack downstream tenga que conocerlo.
_STATUS_MAP = {
    "APPROVED": "aprobado",
    "DECLINED": "rechazado",
    "VOIDED": "rechazado",
    "ERROR": "rechazado",
    "PENDING": "pendiente",
}


class WompiProvider(BasePaymentProvider):
    provider_name = "wompi"

    def __init__(self, public_key: str, private_key: str, events_secret: str, sandbox: bool = True) -> None:
        self.public_key = public_key
        self.private_key = private_key
        self.events_secret = events_secret
        self.base_url = SANDBOX_BASE_URL if sandbox else PRODUCTION_BASE_URL

    async def create_payment_link(
        self, *, reference: str, amount: float, currency: str, expires_in_hours: int
    ) -> PaymentLinkResult:
        if currency == "COP" and amount < MIN_AMOUNT_COP:
            # Chequeo ANTES de llamar a la red: sabemos que Wompi va a rechazar
            # esto (mismo criterio que agent-sale, wompiClient.ts) -- evita una
            # llamada de red que ya sabemos que falla, y devuelve un mensaje
            # accionable en vez de un 422 crudo.
            raise PaymentRequestRejected(
                f"El monto (${amount:,.0f}) es menor al mínimo permitido para pago en línea "
                f"(${MIN_AMOUNT_COP:,.0f} COP). Ofrecé otro método de pago."
            )
        amount_in_cents = round(amount * 100)
        expires_at = (datetime.now(UTC) + timedelta(hours=expires_in_hours)).isoformat()
        body = {
            "name": f"Pago {reference}",
            "description": f"Referencia {reference}",
            "single_use": True,
            # Requerido por Wompi (encontrado en vivo: 422 real,
            # "collect_shipping: No está presente" -- no está en su guía
            # pública, confirmado también en la implementación de agent-sale).
            "collect_shipping": False,
            "currency": currency,
            "amount_in_cents": amount_in_cents,
            "expires_at": expires_at,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/payment_links",
                json=body,
                headers={"Authorization": f"Bearer {self.private_key}"},
            )
        if resp.status_code >= 400:
            # Rechazo de NEGOCIO (validación de Wompi), no una falla del plugin
            # -- PaymentRequestRejected, nunca PluginError acá (ver su docstring
            # en providers/base.py: eso apagaría pack_payments para TODOS los
            # tenants por un rechazo esperable).
            raise PaymentRequestRejected(
                f"Wompi rechazó la creación del link: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json().get("data", {})
        link_id = data.get("id")
        if not link_id:
            raise PaymentRequestRejected("Respuesta de Wompi sin id de link de pago")
        return PaymentLinkResult(
            provider_reference=str(link_id),
            payment_link_url=f"https://checkout.wompi.co/l/{link_id}",
        )

    def verify_webhook_signature(self, payload: dict[str, Any]) -> bool:
        signature = payload.get("signature") or {}
        properties = signature.get("properties") or []
        checksum = signature.get("checksum") or ""
        timestamp = payload.get("timestamp")
        data = payload.get("data") or {}
        if not properties or not checksum or timestamp is None:
            return False

        parts: list[str] = []
        for dotted_path in properties:
            value: Any = data
            for key in dotted_path.split("."):
                if isinstance(value, dict):
                    value = value.get(key)
                else:
                    value = None
                    break
            parts.append(str(value) if value is not None else "")

        concatenated = "".join(parts) + str(timestamp) + self.events_secret
        computed = hashlib.sha256(concatenated.encode()).hexdigest()
        return computed.lower() == str(checksum).lower()

    def parse_webhook_event(self, payload: dict[str, Any]) -> WebhookEvent:
        transaction = (payload.get("data") or {}).get("transaction") or {}
        raw_status = str(transaction.get("status", "")).upper()
        # `transaction.id` es el id de ESTE intento de pago (Wompi genera uno
        # nuevo por cada intento contra el mismo link) -- NO es lo que
        # guardamos como provider_reference al crear el link. Lo que
        # correlaciona de vuelta a NUESTRA fila es `transaction.payment_link_id`
        # (el id del link reusable, `data.id` de la creación). Confirmado
        # contra la implementación real de agent-sale
        # (wompiWebhookHandler.ts::handleWompiWebhook, resuelve por
        # `payment_link_id`, no por `reference` ni por `id`) -- antes de este
        # fix, ESTE campo comparaba mal y ningún webhook real iba a encontrar
        # jamás la transacción que originó el pago.
        return WebhookEvent(
            reference=str(transaction.get("reference", "")),
            provider_reference=str(transaction.get("payment_link_id", "")),
            status=_STATUS_MAP.get(raw_status, "pendiente"),
            raw=payload,
        )
