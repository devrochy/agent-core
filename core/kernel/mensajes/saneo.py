"""Saneo de valores que van dentro de un mensaje (ADR-0108, ADR-0178 D1).

Un valor que entra a un mensaje, venga del evento, de la base o de la persona del equipo, es DATO, nunca
instrucción ni plantilla: aquí se deja apto para Meta (que rechaza el parámetro con 132000/132018 si trae
saltos de línea, tabulaciones o espacios repetidos) y acotado.
"""

from __future__ import annotations

import re

MAX_PARAM = 1024


def clean_param(valor: str, limite: int = MAX_PARAM) -> str:
    """Parámetro apto para Meta: sin saltos ni tabulaciones, sin más de un espacio
    seguido y con largo acotado (Meta rechaza el parámetro, 132000/132018). Se
    aplica a todo valor, venga del evento o de la persona del equipo."""
    texto = re.sub(r"[\r\n\t]+", " ", str(valor))
    texto = re.sub(r" {2,}", " ", texto).strip()
    return texto[:limite].rstrip()
