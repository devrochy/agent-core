"""Períodos por calendario de una suscripción (HU-SUB-04, SPEC-06 § Período por calendario).

La regla vive en el core (`core.subscriptions.periodos`) desde HU-SH-04 (ADR-0123): el cobro
de la plataforma y este pack cortan los períodos en el mismo día. Acá se re-exporta para no
cambiar los imports del pack.
"""

from __future__ import annotations

from core.subscriptions.periodos import (
    MESES,
    PERIODICIDADES,
    etiqueta_periodo,
    meses_de,
    sumar_periodo,
)

__all__ = ["MESES", "PERIODICIDADES", "etiqueta_periodo", "meses_de", "sumar_periodo"]
