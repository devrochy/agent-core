"""Qué tiene que cubrir la firma de un evento de Wompi para confiar en él.

Wompi elige las `signature.properties` de cada evento («pueden variar», docs.wompi.co, «Eventos») y no admite
pedir otras: en `transaction.updated` firma `transaction.id`, `transaction.status` y
`transaction.amount_in_cents`, pero NO `transaction.payment_link_id`. Un checksum válido sobre otras propiedades
deja sin autenticar justo lo que decide el cobro: un evento cuya firma no cubre las tres se rechaza.

Lo usan el webhook de la plataforma (`core.billing.providers.wompi`) y el pack de pagos (invariante #5: los
packs dependen del core, no entre ellos).
"""

from __future__ import annotations

from typing import Any

PROPIEDADES_OBLIGATORIAS = frozenset({"transaction.id", "transaction.status", "transaction.amount_in_cents"})


def firma_cubre_lo_obligatorio(payload: dict[str, Any]) -> bool:
    """¿Las `signature.properties` del evento incluyen el id, el estado y el monto de la transacción?"""
    propiedades = (payload.get("signature") or {}).get("properties") or []
    return isinstance(propiedades, list) and PROPIEDADES_OBLIGATORIAS.issubset(propiedades)


def transaccion_firmada(payload: dict[str, Any]) -> str | None:
    """El `transaction.id` del evento si la firma lo cubre; None si no (o si viene vacío)."""
    if not firma_cubre_lo_obligatorio(payload):
        return None
    valor = ((payload.get("data") or {}).get("transaction") or {}).get("id")
    if valor is None or isinstance(valor, (bool, dict, list)):
        return None
    texto = str(valor).strip()
    return texto or None


__all__ = ["PROPIEDADES_OBLIGATORIAS", "firma_cubre_lo_obligatorio", "transaccion_firmada"]
