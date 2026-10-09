"""Texto que viene de afuera y termina frente a otra persona (HU-SH-03, ADR-0121).

- `contiene_url`: un nombre de negocio con un enlace o un dominio sirve para phishing (va
  en correos y avisos), así que se rechaza donde se pide.
- `enmascarar_correo`: el correo en un aviso de WhatsApp o en un log, sin mostrarlo entero.
"""

from __future__ import annotations

import re

# Esquema, `www.` o algo con forma de dominio (`x.co`, `mi-negocio.com`, el dominio de un
# correo). Una sigla con puntos (`S.A.S.`) o un número (`2.0`) no cuentan.
_URL = re.compile(r"https?://|www\.|\b[a-z0-9][a-z0-9-]*\.[a-z]{2,}\b", re.IGNORECASE)


def contiene_url(texto: object) -> bool:
    return bool(_URL.search(str(texto or "")))


def enmascarar_correo(correo: object) -> str:
    """`ana.perez@correo.co` → `a***@correo.co`. Sin forma de correo → `***`."""
    usuario, arroba, dominio = str(correo or "").strip().partition("@")
    if not arroba or not usuario or not dominio:
        return "***"
    return f"{usuario[0]}***@{dominio}"


__all__ = ["contiene_url", "enmascarar_correo"]
