"""Tarifario de créditos (HU-BILL-04/06, D-35, ADR-0144).

1 crédito = COP 10 de consumo a precio público (`modelo-de-precios.md` §2). Por evento:

    créditos = ⌈costo_usd × TRM × (1 + margen) / 10⌉ (a 0,1) + fee_plataforma(concepto)

- **Margen** 2,0 (precio = 3× el costo) y **fees** fijos por concepto, versionados aquí.
  Cambiar margen o fees es publicar una versión nueva, nunca editar una publicada.
- **TRM** (COP por USD): setting `BILLING_TRM_COP` (por defecto 4000, pendiente del dueño). Cada
  fila del ledger guarda la TRM que usó, así que un cambio de TRM no reescribe lo ya medido.
- **Llave propia (BYOK):** solo el fee (D-05).
- **Meta** queda fuera de los créditos (D-06).

Los créditos de cada fila se calculan **al escribir** la fila de `usage_ledger` y no cambian
después (`creditos_congelados`, la fórmula común de todos los que escriben en el ledger).

**Versiones** (ADR-0208 §14). Cada versión publica desde cuándo rige (`vigente_desde`, UTC, fija en
código, como `DocumentoLegal.vigente_desde`: rige la más reciente que ya empezó).
- `v1` (ADR-0144): rige desde siempre. Las filas viejas se interpretan por su `tarifario_version`, y
  las filas con `credits` NULL (anteriores a 0096) se calculan con `v1`.
- `v2` (ADR-0208 §11-13, HU-BILL-21): mismo margen y mismos fees de turno, voz, imagen y plantilla.
  Lo nuevo es qué se cobra: `agent_run`, `embedding`, `stt`, `vision` y `tts` escriben su fila con
  el costo real más su fee, y desde `vigente_desde` las notas de voz e imágenes dejan de cobrarse
  desde `messages` (`creditos.medir_creditos`). El fee de nota de voz (0,5) y de imagen (1,0) se
  mantiene **por fila del ledger**: es el precio publicado (D-79, `calc_precios.py`) y cubre lo que
  no está en `cost_usd` (descarga del medio, almacenamiento, operación); lo que cambia es la fuente
  (la fila con su costo real en vez del conteo de `messages`). `agent_run`, `embedding` y `tts` no
  llevan fee: ocurren dentro de un turno que ya pagó el suyo, o son ingesta, y D-79 no les publica
  uno (ponerles fee es decisión del dueño y sería otra versión).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from types import MappingProxyType
from typing import Any, Literal

Concepto = Literal["llm_turn", "stt", "vision", "template_sent", "agent_run", "embedding", "tts"]
KeyMode = Literal["platform", "byok"]

CREDITO_COP = Decimal(10)
_DECIMA = Decimal("0.1")


@dataclass(frozen=True)
class Tarifario:
    version: str
    margen: Decimal
    fees: Mapping[str, Decimal]
    # Desde cuándo rige (UTC). None = desde siempre (solo `v1`).
    vigente_desde: datetime | None = None


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
TARIFARIO_V2 = Tarifario(
    version="v2",
    margen=Decimal("2.0"),
    fees=MappingProxyType({
        "llm_turn": Decimal("0.2"),
        "stt": Decimal("0.5"),
        "vision": Decimal("1.0"),
        "template_sent": Decimal("0.3"),
        "agent_run": Decimal("0.0"),
        "embedding": Decimal("0.0"),
        "tts": Decimal("0.0"),
    }),
    # Día del merge de HU-BILL-21. Tiene que ser ANTERIOR o igual al despliegue, nunca posterior: entre
    # el despliegue y esta fecha una nota de voz pagaría su fila real y además el fee de `messages`
    # (cobro doble). Si es anterior, la ventana hasta el despliegue solo cobra de menos (filas en sombra
    # del código viejo y sin fee de `messages`), y eso lo absorbe la plataforma.
    vigente_desde=datetime(2026, 11, 22, tzinfo=UTC),
)
TARIFARIO_VIGENTE = TARIFARIO_V2


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


def creditos_congelados(
    cost_usd: Decimal | float,
    concepto: Concepto,
    key_mode: KeyMode = "platform",
    *,
    hubo_gasto: bool = True,
) -> dict[str, Any]:
    """Columnas de créditos de una fila de `usage_ledger` al escribirla (ADR-0144 §3, ADR-0208 §11):
    `credits`, `key_mode`, `tarifario_version` y `trm_cop`, con el tarifario y la TRM vigentes. Van
    las cuatro juntas (`ck_usage_credits_completos`, migración 0096). Sin gasto (`hubo_gasto=False`,
    p. ej. un turno sin llamada al LLM) los créditos son 0: no hay evento que cobrar.

    Es la única fórmula «costo → créditos congelados»: turno, operación entre agentes, embeddings,
    notas de voz, imágenes y voz de salida la usan para no repetirla."""
    key_mode = "byok" if key_mode == "byok" else "platform"
    trm = trm_vigente()
    costo = Decimal(str(cost_usd))
    credits = creditos_por_evento(costo, concepto, key_mode, trm) if hubo_gasto else Decimal("0.0")
    return {
        "credits": credits,
        "key_mode": key_mode,
        "tarifario_version": TARIFARIO_VIGENTE.version,
        "trm_cop": trm,
    }


__all__ = [
    "CREDITO_COP",
    "TARIFARIO_V1",
    "TARIFARIO_V2",
    "TARIFARIO_VIGENTE",
    "Concepto",
    "KeyMode",
    "Tarifario",
    "creditos_congelados",
    "creditos_por_evento",
    "creditos_variables",
    "trm_vigente",
]
