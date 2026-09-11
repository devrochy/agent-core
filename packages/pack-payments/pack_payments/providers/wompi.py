"""Proveedor Wompi (pasarela de pagos, Colombia). Sin SDK: httpx nativo.

Verificación de firma de webhook: confirmada contra la documentación
pública de Wompi (docs.wompi.co/docs/colombia/eventos) — concatenar los
valores de `signature.properties` (en el orden dado, sin separador) +
`timestamp` + el `events_secret` del tenant, SHA256, comparar contra
`signature.checksum`. Este algoritmo SÍ está verificado.

El body exacto de creación de un link de pago (`POST /v1/payment_links`)
está implementado según la forma pública conocida de la API de Wompi
(amount_in_cents, single_use, currency, name, redirect_url), pero no se
pudo verificar en vivo contra la documentación al escribir esto (la página
devolvió 403 a la herramienta de fetch). **Validar contra el sandbox real
de Wompi antes de procesar un cobro real** — si el shape difiere, el único
lugar a tocar es `create_payment_link` de este archivo.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from pack_payments.providers.base import BasePaymentProvider, PaymentLinkResult, WebhookEvent

log = get_logger(__name__)

PRODUCTION_BASE_URL = "https://production.wompi.co/v1"
SANDBOX_BASE_URL = "https://sandbox.wompi.co/v1"

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
        amount_in_cents = round(amount * 100)
        body = {
            "name": f"Pago {reference}",
            "description": f"Referencia {reference}",
            "single_use": True,
            "currency": currency,
            "amount_in_cents": amount_in_cents,
            "reference": reference,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.base_url}/payment_links",
                json=body,
                headers={"Authorization": f"Bearer {self.private_key}"},
            )
        if resp.status_code >= 400:
            raise PluginError("pack_payments", f"Wompi rechazó la creación del link: {resp.status_code} {resp.text[:300]}")
        data = resp.json().get("data", {})
        link_id = data.get("id")
        if not link_id:
            raise PluginError("pack_payments", "Respuesta de Wompi sin id de link de pago")
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
        return WebhookEvent(
            reference=str(transaction.get("reference", "")),
            provider_reference=str(transaction.get("id", "")),
            status=_STATUS_MAP.get(raw_status, "pendiente"),
            raw=payload,
        )
