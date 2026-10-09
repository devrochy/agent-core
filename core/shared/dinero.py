"""Montos de dinero: cómo se escriben en una respuesta y dónde los trae una tool.

Lo usan el verificador de afirmaciones del core (ADR-0119, HU-ARCH-29) y los
packs. Antes vivía en pack-commerce (`crm_amount_check`); las claves de
`MONEY_KEYS` son una convención del core para los packs (docs/PLUGIN-CONTRACT.md
§11): un pack que devuelve un monto verificado lo pone bajo una de esas claves.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

MONEY_IN_TEXT = re.compile(r"\$\s?[\d][\d.,]*|\bCOP\s?[\d][\d.,]*")

# Claves bajo las que un resultado de tool puede traer un monto real (puerto
# directo de MONEY_KEYS/walkForKeys de agent-sale, src/shared/observability/
# priceGuardrail.ts) -- por NOMBRE de clave, no "cualquier número", para no
# confundir un stock/quantity numérico con un monto verificado.
MONEY_KEYS = {
    "price", "unit_price", "line_total", "subtotal", "total", "discount",
    # Montos que el SERVIDOR pone en las tools de suscripción (core): `monto` del cargo y
    # `valor_periodo` del plan. Sin ellos, mencionar el valor de una suscripción o la
    # diferencia de un cambio de plan se bloqueaba y escalaba (journeys HU-SUB-12/07).
    # `amount` NO: `generar_link_pago` lo devuelve tal como lo pasó el LLM (auditoría).
    "monto", "valor_periodo",
}


def parse_money(text: str, locale: str = "es-CO") -> float | None:
    """Interpreta un monto de texto en formato del negocio (es-CO por defecto).

    Acepta `$1.800.000`, `1.800.000`, `1.800.000,50`, `COP 1.800.000`,
    `$ 15` y `15`. Devuelve `None` si no hay un número reconocible.

    Regla de separadores (es-CO): el punto es de miles y la coma es decimal.
    Si solo hay puntos repetidos (`1.800.000`) o un punto con 3 dígitos detrás,
    se tratan como miles; si hay coma, la coma es el decimal.
    """
    if not text:
        return None
    # Quita símbolos y espacios (incluye espacio fino/no separable).
    cleaned = re.sub(r"[^\d.,]", "", text).strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace("\u00a0", "").replace("\u202f", "")

    if "." in cleaned and "," in cleaned:
        # El separador decimal es el que aparece más a la derecha; el otro es de miles.
        if cleaned.rfind(",") > cleaned.rfind("."):
            normalized = cleaned.replace(".", "").replace(",", ".")
        else:
            normalized = cleaned.replace(",", "")
    elif "," in cleaned:
        # Varias comas, o la última parte con != 2 dígitos -> son de miles.
        partes = cleaned.split(",")
        decimal = len(partes) == 2 and 1 <= len(partes[-1]) <= 2
        normalized = cleaned.replace(",", ".") if decimal else cleaned.replace(",", "")
    else:
        normalized = cleaned
        # Puntos: varios, o uno con exactamente 3 dígitos detrás -> miles.
        if "." in normalized:
            partes = normalized.split(".")
            if len(partes) > 2 or (len(partes) == 2 and len(partes[1]) == 3):
                normalized = normalized.replace(".", "")

    try:
        valor = float(Decimal(normalized))
    except (InvalidOperation, ValueError):
        return None
    # Una cifra absurda ("$1" seguido de 400 ceros) desborda a `inf`: no es un monto.
    return valor if math.isfinite(valor) else None


def normalized_amount_key(value: float) -> str:
    """Clave de comparación de un monto: valor con 2 decimales, sin notación
    científica (arregla el bloqueo de montos ≥ 1e6, HU-COM-02)."""
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def extract_monetary_values(node: Any, key: str | None = None) -> list[float]:
    """Recorre `node` recursivamente (listas y dicts anidados) y colecciona los
    números que aparecen bajo una clave de `MONEY_KEYS`.

    Antes, el guardrail de montos de pack-commerce solo miraba los valores de primer nivel del
    dict de resultado de cada tool -- `consultar_inventario`/`recomendar_producto`
    devuelven el precio anidado dentro de `matches`/`recommendations` (una
    lista), así que el guardrail nunca podía verificarlo y bloqueaba CUALQUIER
    respuesta que mencionara un precio de catálogo real (encontrado en vivo:
    tenant yopoldo, "Pollo Broaster" a $60.000 escalaba siempre, no de forma
    intermitente). El escaneo por nombre de clave (no "cualquier número")
    también evita que un `stock`/`quantity` se cuele como monto verificado.
    """
    values: list[float] = []
    if isinstance(node, list):
        for item in node:
            values.extend(extract_monetary_values(item))
    elif isinstance(node, dict):
        for child_key, child_value in node.items():
            values.extend(extract_monetary_values(child_value, child_key))
    elif (
        isinstance(node, (int, float))
        and not isinstance(node, bool)
        and key is not None
        and key in MONEY_KEYS
        # `json.loads` acepta NaN e Infinity (una tool declarativa o un MCP externo).
        and math.isfinite(node)
    ):
        values.append(float(node))
    return values
