"""Proveedor Wompi para la suscripción de plataforma (ADR-0046).

Cliente httpx nativo, sin SDK. Replica el algoritmo YA VERIFICADO contra el
sandbox real de `packages/pack-payments/providers/wompi.py`:
- body de `POST /v1/payment_links` (incluye `collect_shipping: false`, que Wompi
  exige aunque no está en la guía pública);
- verificación de firma del webhook (concatenar `signature.properties` en orden
  + `timestamp` + `events_secret`, SHA256);
- correlación del webhook por `transaction.payment_link_id` (NO por `id`, que
  cambia en cada intento) — es lo que se guarda al crear el link;
- mínimo real de Wompi de 150.000 COP por link.

La duplicación con el pack es deliberada y documentada en el ADR: core no puede
importar un pack y el dominio (cobro de plataforma) es otro.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.billing.providers.base import (
    BaseBillingProvider,
    BillingRequestRejected,
    BillingWebhookEvent,
    PaymentLink,
)
from core.shared.logger import get_logger

log = get_logger(__name__)

PRODUCTION_BASE_URL = "https://production.wompi.co/v1"
SANDBOX_BASE_URL = "https://sandbox.wompi.co/v1"

# Mínimo real de Wompi para la base de una transacción en COP (no está en su
# guía pública; verificado por QA contra el sandbox en pack-payments).
MIN_AMOUNT_COP = 150_000

_STATUS_MAP = {
    "APPROVED": "aprobado",
    "DECLINED": "rechazado",
    "VOIDED": "rechazado",
    "ERROR": "rechazado",
    "PENDING": "pendiente",
}


class WompiBillingProvider(BaseBillingProvider):
    provider_name = "wompi"

    def __init__(
        self,
        *,
        public_key: str,
        private_key: str,
        events_secret: str,
        sandbox: bool = True,
    ) -> None:
        self.public_key = public_key
        self.private_key = private_key
        self.events_secret = events_secret
        self.base_url = SANDBOX_BASE_URL if sandbox else PRODUCTION_BASE_URL

    async def create_payment_link(
        self, *, reference: str, amount_cop: float, expires_in_hours: int = 24,
        plan_name: str = "",
    ) -> PaymentLink:
        if amount_cop < MIN_AMOUNT_COP:
            # Chequeo ANTES de la red: Wompi va a rechazar esto. Mensaje
            # accionable en vez de un 422 crudo.
            raise BillingRequestRejected(
                f"El plan cuesta menos que el mínimo de Wompi (${MIN_AMOUNT_COP:,.0f} COP). "
                "Cobrá trimestral o anual, o ajustá el precio del plan."
            )
        amount_in_cents = round(amount_cop * 100)
        expires_at = (datetime.now(UTC) + timedelta(hours=expires_in_hours)).isoformat()
        # Nombre visible al cliente: el plan y "SolversHub", NUNCA un nombre
        # técnico (HU-REP-04).
        visible_plan = plan_name.strip() or "plan"
        body = {
            "name": f"{visible_plan} · SolversHub",
            "description": f"Suscripción a SolversHub — plan {visible_plan}",
            "single_use": True,
            "collect_shipping": False,  # Wompi lo exige (422 real si falta)
            "currency": "COP",
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
            raise BillingRequestRejected(
                f"Wompi rechazó el link de pago: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json().get("data", {})
        link_id = data.get("id")
        if not link_id:
            raise BillingRequestRejected("Respuesta de Wompi sin id de link de pago")
        return PaymentLink(
            provider_reference=str(link_id),
            url=f"https://checkout.wompi.co/l/{link_id}",
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
                value = value.get(key) if isinstance(value, dict) else None
                if value is None:
                    break
            parts.append(str(value) if value is not None else "")

        concatenated = "".join(parts) + str(timestamp) + self.events_secret
        computed = hashlib.sha256(concatenated.encode()).hexdigest()
        # Comparación en tiempo constante (HU-SH-04): no filtra por tiempos cuánto coincide.
        return hmac.compare_digest(computed.lower(), str(checksum).lower())

    def parse_webhook_event(self, payload: dict[str, Any]) -> BillingWebhookEvent:
        transaction = (payload.get("data") or {}).get("transaction") or {}
        raw_status = str(transaction.get("status", "")).upper()
        # `payment_link_id` es el id del link reusable (lo que guardamos);
        # `id` cambia en cada intento y NO correlaciona (bug real ya visto).
        return BillingWebhookEvent(
            provider_reference=str(transaction.get("payment_link_id", "")),
            status=_STATUS_MAP.get(raw_status, "pendiente"),
            amount_in_cents=transaction.get("amount_in_cents"),
            raw=payload,
        )
