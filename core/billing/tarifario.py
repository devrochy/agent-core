"""Tarifario de créditos (HU-BILL-04/06, D-35, ADR-0144).

1 crédito = COP 10 de consumo a precio público (`modelo-de-precios.md` §2). Por evento:

    créditos = ⌈costo_usd × TRM × (1 + margen) / 10⌉ (a 0,1) + fee_plataforma(concepto)

- **Margen** 2,0 (precio = 3× el costo) y **fees** fijos por concepto, versionados aquí (`v1`).
  Cambiar margen o fees es publicar una versión nueva, nunca editar `v1`.
- **TRM** (COP por USD): setting `BILLING_TRM_COP` (por defecto 4000, pendiente del dueño). Cada
  fila del ledger guarda la TRM que usó, así que un cambio de TRM no reescribe lo ya medido.
- **Llave propia (BYOK):** solo el fee (D-05).
- **Meta** queda fuera de los créditos (D-06).

Los créditos de un turno se calculan **al escribir** la fila de `usage_ledger` y no cambian después.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from types import MappingProxyType
from typing import Literal

Concepto = Literal["llm_turn", "stt", "vision", "template_sent"]
KeyMode = Literal["platform", "byok"]

CREDITO_COP = Decimal(10)
_DECIMA = Decimal("0.1")


@dataclass(frozen=True)
class Tarifario:
    version: str
    margen: Decimal
    fees: Mapping[str, Decimal]


TARIFARIO_V1 = Tarifario(
    version="v1",
    margen=Decimal("2.0"),
    # Solo lectura: una mutación accidental cambiaría la tarifa de todos los negocios.
    fees=MappingProxyType({
        "llm_turn": Decimal("0.2"),
        "stt": Decimal("0.5"),
        "vision": Decimal("1.0"),
        "template_sent": Decimal("0.3"),
    }),
)
TARIFARIO_VIGENTE = TARIFARIO_V1


def trm_vigente() -> Decimal:
    """COP por USD del setting `BILLING_TRM_COP` (ADR-0144), a 2 decimales: la misma precisión
    con que se guarda en `usage_ledger.trm_cop`, para que la fila reproduzca sus créditos."""
    from core.config import get_settings

    return Decimal(str(get_settings().billing_trm_cop)).quantize(Decimal("0.01"))


def creditos_variables(cost_usd: Decimal | float, trm: Decimal, tarifario: Tarifario = TARIFARIO_VIGENTE) -> Decimal:
    """Parte variable: ⌈costo_usd × TRM × (1 + margen) / 10⌉ a 0,1. Nunca negativa."""
    costo = Decimal(str(cost_usd))
    if costo <= 0:
        return Decimal("0.0")
    bruto = costo * trm * (1 + tarifario.margen) / CREDITO_COP
    return bruto.quantize(_DECIMA, rounding=ROUND_CEILING)


def creditos_por_evento(
    cost_usd: Decimal | float,
    concepto: Concepto,
    key_mode: KeyMode,
    trm: Decimal,
    tarifario: Tarifario = TARIFARIO_VIGENTE,
) -> Decimal:
    """Créditos de un evento. Con BYOK solo se cobra el fee."""
    fee = tarifario.fees[concepto]
    if key_mode == "byok":
        return fee
    return creditos_variables(cost_usd, trm, tarifario) + fee


__all__ = [
    "CREDITO_COP",
    "TARIFARIO_V1",
    "TARIFARIO_VIGENTE",
    "Concepto",
    "KeyMode",
    "Tarifario",
    "creditos_por_evento",
    "creditos_variables",
    "trm_vigente",
]
