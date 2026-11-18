"""Mínimo de Wompi por ambiente, compartido por el pago en línea y el cobro de plataforma.

Vive en el core porque lo usan `pack_payments` (pago en línea del negocio) y
`core.billing.providers.wompi` (suscripción de plataforma), y el core no puede
importar un pack.
"""

from __future__ import annotations

# Mínimo del sandbox de Wompi para la base de una transacción en COP: no está
# en su guía pública, lo confirmó agent-sale por QA contra el sandbox real
# (mismo 422 que si no se chequea antes de llamar).
SANDBOX_MIN_AMOUNT_COP = 150_000
# Mínimo publicado para producción (modelo Agregador; Gateway admite desde $1).
PRODUCTION_MIN_AMOUNT_COP = 1_500


def min_amount_cop(sandbox: bool) -> int:
    """Mínimo en COP que Wompi acepta en el ambiente dado."""
    return SANDBOX_MIN_AMOUNT_COP if sandbox else PRODUCTION_MIN_AMOUNT_COP
