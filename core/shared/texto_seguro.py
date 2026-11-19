"""Texto que viene de afuera y termina frente a otra persona (HU-SH-03, ADR-0121).

- `contiene_url`: un nombre de negocio con un enlace o un dominio sirve para phishing (va
  en correos y avisos), así que se rechaza donde se pide.
- `enmascarar_correo`: el correo en un aviso de WhatsApp o en un log, sin mostrarlo entero.
"""

from __future__ import annotations

import re
import unicodedata

# Puntos que un navegador acepta como separador de dominio (`evil\u3002com`) y que NFKC no deja
# como ".": el ideográfico (U+3002) y, por si llegan sin normalizar, el de ancho completo
# (U+FF0E) y el de medio ancho (U+FF61).
_PUNTOS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})
# Esquema, `www.` o algo con forma de dominio en cualquier alfabeto: una etiqueta de letras o
# números (`pаypal`, con «а» cirílica, cuenta), un punto y un final de 2+ letras (`com`, `co`,
# `рф`). Una sigla con puntos (`S.A.S.`) o un número (`2.0`) no cuentan.
_URL = re.compile(r"https?://|www\.|\b[^\W_][\w-]*\.[^\W\d_]{2,}\b", re.IGNORECASE)


def _normalizado(texto: object) -> str:
    """NFKC (letras y signos de ancho completo a su forma común) y los puntos alternativos."""
    return unicodedata.normalize("NFKC", str(texto or "")).translate(_PUNTOS)


def contiene_url(texto: object) -> bool:
    return bool(_URL.search(_normalizado(texto)))


def enmascarar_correo(correo: object) -> str:
    """`ana.perez@correo.co` → `a***@correo.co`. Sin forma de correo → `***`."""
    usuario, arroba, dominio = str(correo or "").strip().partition("@")
    if not arroba or not usuario or not dominio:
        return "***"
    return f"{usuario[0]}***@{dominio}"


__all__ = ["contiene_url", "enmascarar_correo"]
