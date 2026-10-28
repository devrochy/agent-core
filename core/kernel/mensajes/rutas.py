"""Rutas de datos de un mensaje (`cliente.nombre`) y su resolución contra un contexto (ADR-0108, ADR-0178 D1).

Una ruta solo se resuelve si el llamador la PERMITE (lista cerrada: `permitidas`). El contexto lo arma el
servidor; nunca sale del texto del LLM ni de lo que escribe el cliente.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

from core.kernel.mensajes.saneo import clean_param

# Forma de una ruta de variable en una plantilla con opciones (hasta 4 puntos).
RUTA_VARIABLE = re.compile(r"^[a-z][a-z0-9_]{0,29}(\.[a-z][a-z0-9_]{0,29}){0,3}$")


class RutaNoPermitida(ValueError):
    """La ruta no está en la lista permitida del llamador."""

    def __init__(self, ruta: str) -> None:
        super().__init__(ruta)
        self.ruta = ruta


class DatoFaltante(ValueError):
    """La ruta está permitida pero el contexto no trae un valor simple para ella."""

    def __init__(self, ruta: str) -> None:
        super().__init__(ruta)
        self.ruta = ruta


def valor_de_ruta(contexto: Mapping[str, Any], ruta: str) -> str | None:
    """El valor de `ruta` (`a.b`) en el contexto anidado, saneado con `clean_param`. None si falta, es
    None, un objeto o una lista, o queda vacío. El contexto y cada nivel deben ser `dict` (como los arma el
    servidor); otro tipo de mapeo cuenta como dato ausente."""
    actual: Any = contexto
    for parte in ruta.split("."):
        if not isinstance(actual, dict) or parte not in actual:
            return None
        actual = actual[parte]
    if actual is None or isinstance(actual, dict | list):
        return None
    texto = clean_param(str(actual))
    return texto or None


def resolver_ruta(contexto: Mapping[str, Any], ruta: str, permitidas: Collection[str]) -> str:
    """Valor saneado de `ruta`. `RutaNoPermitida` si no está en `permitidas`; `DatoFaltante` si no hay dato."""
    if ruta not in permitidas:
        raise RutaNoPermitida(ruta)
    valor = valor_de_ruta(contexto, ruta)
    if valor is None:
        raise DatoFaltante(ruta)
    return valor
