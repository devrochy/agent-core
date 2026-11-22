"""Celdas de CSV a prueba de inyección de fórmulas (hojas de cálculo).

Lo usan las exportaciones del panel (clientes, conversaciones): una celda que empieza por `=`, `+`,
`-`, `@`, tabulador o retorno de carro se antepone con `'` para que Excel o Sheets no la ejecuten.
"""

from __future__ import annotations

from typing import Any


def celda(valor: Any) -> str:
    """Texto de una celda, neutralizando fórmulas (inyección CSV en hojas de cálculo)."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto[:1] in ("=", "+", "-", "@", "\t", "\r") else texto
