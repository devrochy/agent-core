"""Diferencia prorrateada de un upgrade (HU-SUB-07, ADR-0111).

`(precio nuevo − precio actual) × días que faltan del período / días del período`, por
días de calendario y redondeada al peso (COP) o al centavo (otras monedas). Un downgrade
o un cambio al mismo precio no generan cobro.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal


def diferencia_prorrateada(
    actual: Decimal, nuevo: Decimal, inicio: datetime, fin: datetime, ahora: datetime, moneda: str = "COP"
) -> Decimal:
    actual, nuevo = Decimal(str(actual)), Decimal(str(nuevo))
    if nuevo <= actual:
        return Decimal("0")
    dias_periodo = max((fin.date() - inicio.date()).days, 1)
    dias_restantes = min(max((fin.date() - ahora.date()).days, 0), dias_periodo)
    monto = (nuevo - actual) * Decimal(dias_restantes) / Decimal(dias_periodo)
    paso = Decimal("1") if (moneda or "COP").upper() == "COP" else Decimal("0.01")
    return monto.quantize(paso, rounding=ROUND_HALF_UP)
