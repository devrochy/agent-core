"""Proveedor Wompi (pasarela de pagos, Colombia). Sin SDK.

Salida a red (ADR-0052): el pack no usa `httpx` directo. El panel ("Probar y
guardar") y la capacidad de pagos usan `plugin_http(NETWORK)` (`NETWORK` es la
`network_allowlist` del módulo). Un fallo
de red o un dominio fuera de la allowlist es `PluginError`, como antes la
excepción de `httpx`; un 4xx/5xx de Wompi sigue siendo `PaymentRequestRejected`.

Verificación de firma de webhook: confirmada contra la documentación
pública de Wompi (docs.wompi.co/docs/colombia/eventos) — concatenar los
valores de `signature.properties` (en el orden dado, sin separador) +
`timestamp` + el `events_secret` del tenant, SHA256, comparar contra
`signature.checksum`. Este algoritmo SÍ está verificado.

El body de creación de un link de pago (`POST /v1/payment_links`) se
verificó contra el sandbox REAL de Wompi (encontrado en vivo, tenant
yopoldo, 2026-09-11: un 422 real por `collect_shipping` faltante) y contra
la implementación ya probada de `agent-sale`
(`src/payments/wompiClient.ts`).

Mínimo del monto: el de 150.000 COP salió de un 422 del SANDBOX ("La base de
la transacción debe ser igual o mayor a 150000"), no de la documentación. En
producción el mínimo publicado por Wompi es 1.500 COP en el modelo Agregador
(y $1 en Gateway), según soporte.wompi.co ("¿Cuál es el monto mínimo para
realizar una transacción?", consultado 2026-10-08). Por eso el chequeo
depende del ambiente.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from core.mcp.types import ScopedHttp, plugin_http
from core.shared.exceptions import PluginError
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
# Hosts a los que el servidor sale. `checkout.wompi.co` (manifest) solo lo abre
# el navegador del pagador: el link se arma, no se llama.
NETWORK = ("production.wompi.co", "sandbox.wompi.co")
TIMEOUT = 15.0
PLUGIN_ID = "pack_payments"

# Mínimo del sandbox de Wompi para la base de una transacción en COP: no está
# en su guía pública, lo confirmó agent-sale por QA contra el sandbox real
# (mismo 422 que si no se chequea acá antes de llamar).
SANDBOX_MIN_AMOUNT_COP = 150_000
# Mínimo publicado para producción (modelo Agregador; Gateway admite desde $1).
PRODUCTION_MIN_AMOUNT_COP = 1_500


def min_amount_cop(sandbox: bool) -> int:
    """Mínimo en COP que Wompi acepta en el ambiente dado."""
    return SANDBOX_MIN_AMOUNT_COP if sandbox else PRODUCTION_MIN_AMOUNT_COP

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

    def __init__(
        self,
        public_key: str,
        private_key: str,
        events_secret: str,
        sandbox: bool = True,
        http: ScopedHttp | None = None,
    ) -> None:
        self.public_key = public_key
        self.private_key = private_key
        self.events_secret = events_secret
        self.base_url = SANDBOX_BASE_URL if sandbox else PRODUCTION_BASE_URL
        self.min_amount_cop = min_amount_cop(sandbox)
        # Sin `http` explícito (panel, capacidad), el cliente del pack.
        self.http = http or plugin_http(NETWORK)

    async def create_payment_link(
        self, *, reference: str, amount: float, currency: str, expires_in_hours: int
    ) -> PaymentLinkResult:
        if currency == "COP" and amount < self.min_amount_cop:
            # Chequeo ANTES de llamar a la red: sabemos que Wompi va a rechazar
            # esto (mismo criterio que agent-sale, wompiClient.ts) -- evita una
            # llamada de red que ya sabemos que falla, y devuelve un mensaje
            # accionable en vez de un 422 crudo.
            raise PaymentRequestRejected(
                f"El monto (${amount:,.0f}) es menor al mínimo permitido para pago en línea "
                f"(${self.min_amount_cop:,.0f} COP). Ofrecé otro método de pago."
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
        result = await self.http.request(
            "POST",
            f"{self.base_url}/payment_links",
            json_body=body,
            headers={"Authorization": f"Bearer {self.private_key}"},
            timeout=TIMEOUT,
        )
        if "error" in result:
            # No llegó a Wompi (red, dominio fuera de la allowlist): falla de
            # infraestructura, no rechazo de negocio.
            raise PluginError(PLUGIN_ID, f"Wompi: {result['error']}")
        status = int(result["status"])
        payload = result["data"]
        if status >= 400:
            # Rechazo de NEGOCIO (validación de Wompi), no una falla del plugin
            # -- PaymentRequestRejected, nunca PluginError acá (ver su docstring
            # en providers/base.py: eso apagaría pack_payments para TODOS los
            # tenants por un rechazo esperable).
            raise PaymentRequestRejected(
                f"Wompi rechazó la creación del link: {status} {_snippet(payload)}"
            )
        data = (payload.get("data") or {}) if isinstance(payload, dict) else {}
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
            amount=_signed_amount(payload, transaction),
        )


def _snippet(data: Any, limit: int = 300) -> str:
    """Primeros caracteres del cuerpo de la respuesta (para el mensaje de rechazo)."""
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return text[:limit]


def _signed_amount(payload: dict[str, Any], transaction: dict[str, Any]) -> float | None:
    """Monto pagado (`amount_in_cents` / 100), solo si la firma lo cubre.

    Wompi avisa que `signature.properties` "pueden variar" por evento
    (docs.wompi.co/docs/colombia/eventos): si `transaction.amount_in_cents` no
    está entre las propiedades firmadas, el monto no está autenticado y se
    devuelve `None` (el que confirma el pago lo trata como no coincidente).
    """
    properties = (payload.get("signature") or {}).get("properties") or []
    if "transaction.amount_in_cents" not in properties:
        return None
    cents = transaction.get("amount_in_cents")
    if isinstance(cents, bool) or cents is None:
        return None
    try:
        value = Decimal(str(cents))
    except InvalidOperation:
        return None
    if not value.is_finite() or value != value.to_integral_value() or value < 0:
        return None
    return float(value / 100)
