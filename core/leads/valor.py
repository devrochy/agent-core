"""Valor estimado del lead (HU-CRM-23): leerlo, validarlo y agruparlo en tramos para filtrar.

Es un monto en la moneda del negocio, sin conversión, con hasta dos decimales y nunca negativo. Se escribe
como número simple: sin separador de miles, porque «150.000» es 150 mil en Colombia y 150 en otros países,
y adivinar guardaría un valor mil veces mal. Vacío lo quita.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, case, literal

# numeric(12,2): diez dígitos enteros y dos decimales.
VALOR_MAX = Decimal("9999999999.99")
_SIMPLE = re.compile(r"^\d+(?:[.,]\d{1,2})?$")

MENSAJE_INVALIDO = (
    "El valor estimado debe ser un número de cero en adelante, sin puntos de miles ni símbolos "
    "(por ejemplo 250000 o 250000.50)."
)

# Tramos para filtrar la lista. Los cortes son montos de la moneda del negocio; hoy, pesos.
SIN_VALOR = "sin_valor"
TRAMOS: tuple[tuple[str, str, Decimal | None, Decimal | None], ...] = (
    ("hasta_100000", "Hasta $100.000", None, Decimal("100000")),
    ("100000_500000", "$100.000 a $500.000", Decimal("100000"), Decimal("500000")),
    ("500000_2000000", "$500.000 a $2.000.000", Decimal("500000"), Decimal("2000000")),
    ("mas_de_2000000", "Más de $2.000.000", Decimal("2000000"), None),
)
OPCIONES: tuple[tuple[str, str], ...] = (*[(c, e) for c, e, _, _ in TRAMOS], (SIN_VALOR, "Sin valor"))


def parsear(texto: Any) -> Decimal | None:
    """El valor escrito como `Decimal`, o `None` si está vacío. `ValueError` si no es un monto válido."""
    crudo = str(texto).strip() if texto is not None else ""
    if not crudo:
        return None
    if not _SIMPLE.match(crudo):
        raise ValueError(MENSAJE_INVALIDO)
    try:
        valor = Decimal(crudo.replace(",", "."))
    except InvalidOperation as exc:  # pragma: no cover  (el patrón ya lo descarta)
        raise ValueError(MENSAJE_INVALIDO) from exc
    if valor > VALOR_MAX:
        raise ValueError(f"El valor estimado admite hasta {VALOR_MAX:,.0f}.".replace(",", "."))
    return valor


def texto(valor: Decimal | None) -> str:
    """Para el campo editable: sin ceros de más («250000», «250000.5»), vacío si no hay valor."""
    if valor is None:
        return ""
    return format(valor.normalize(), "f")


def condicion(clave: str, columna: Any) -> Any:
    """Condición SQL del tramo `clave` sobre la columna `estimated_value`. Un lead sin valor solo cae en
    `sin_valor`; los cortes son semiabiertos (100.000 exacto va en el tramo siguiente)."""
    if clave == SIN_VALOR:
        return columna.is_(None)
    for codigo, _, desde, hasta in TRAMOS:
        if codigo != clave:
            continue
        partes = [columna.is_not(None)]
        if desde is not None:
            partes.append(columna >= desde)
        if hasta is not None:
            partes.append(columna < hasta)
        return and_(*partes)
    return literal(False)


def tramo_sql(columna: Any) -> Any:
    """El tramo de cada lead como expresión SQL (para contar las opciones del filtro)."""
    return case(
        *[(condicion(codigo, columna), literal(codigo)) for codigo, *_ in TRAMOS],
        else_=literal(SIN_VALOR),
    )
