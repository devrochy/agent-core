"""La respuesta de texto a un menú numerado (SPEC-40, pista C2, ADR-0172 adenda): qué opción eligió el cliente.

En un canal sin botones el envío de una plantilla con opciones sale como texto («1. título», `mapeo.texto_de_respaldo`)
y el cliente contesta con un mensaje. Este módulo decide, de forma PURA y estricta, si ese mensaje es una elección:

- un número entero de 1 a N, solo («1», «1.», «1)», « 1 »), sin ceros a la izquierda; o
- el título EXACTO de una opción, normalizado (espacios colapsados, mayúsculas y minúsculas indiferentes, NFKC).

Cualquier otra cosa es `None` y el mensaje sigue su camino de siempre (el agente): «1 por favor», «quiero la 1»,
un número fuera de rango, un título parcial o dos opciones con el mismo título normalizado (ambiguo). Un mensaje con
forma de número NUNCA se interpreta como título (un «5» con 3 opciones no es el título «5»).

Qué más debe cumplirse para que una elección se ejecute (menú vigente y último saliente, bandera, capacidad del canal,
firma) vive en `core/orchestrator/opciones_toque.py`; esto solo lee el texto. Puro: ni base, ni red, ni entorno.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

# Largo máximo de un mensaje que puede ser una elección: el título más largo que admite la definición (100).
# Un mensaje más largo ni se compara (y el consumer lo descarta antes de consultar nada).
MAX_LARGO_ELECCION = 100

_NUMERO = re.compile(r"\s*([1-9][0-9]?)\s*[.)]?\s*")


def normalizar(texto: str) -> str:
    """Forma comparable de un título o de lo que escribió el cliente: NFKC, espacios colapsados, sin distinguir
    mayúsculas. No quita acentos ni signos: «exacto» es exacto salvo mayúsculas y espacios."""
    return " ".join(unicodedata.normalize("NFKC", texto).split()).casefold()


def elegir(texto: str, titulos: Sequence[str]) -> int | None:
    """Índice (base 0) de la opción elegida, o `None` si el texto no es una elección inequívoca de este menú."""
    if not texto or len(texto) > MAX_LARGO_ELECCION or not titulos:
        return None
    m = _NUMERO.fullmatch(texto)
    if m is not None:
        n = int(m.group(1))
        return n - 1 if 1 <= n <= len(titulos) else None
    buscado = normalizar(texto)
    if not buscado:
        return None
    coincidencias = [i for i, t in enumerate(titulos) if normalizar(t) == buscado]
    return coincidencias[0] if len(coincidencias) == 1 else None
