"""SafeRenderer: datos sensibles NUNCA pasan por el LLM.

Patrón heredado de agent-sale (datos bancarios, links de pago): la tool
devuelve el texto/URL exacto en claves reservadas del resultado y el core
lo adjunta DETERMINÍSTICAMENTE a la respuesta final. El LLM puede anunciar
"te envío los datos", pero el contenido exacto lo pone el core.

Claves reservadas en el resultado de una tool:
  _safe_append : str  -> texto que se agrega verbatim al final de la respuesta.
  _image_url   : str  -> imagen que se adjunta a la última burbuja.
  _tool_name   : str  -> nombre de la tool que produjo este resultado (inyectado
                          por el core, no por el pack) -- lo usan guardrails
                          como `crm_completion_check` para verificar que una
                          afirmación de "ya lo hice" en el texto corresponda a
                          una tool REALMENTE llamada en este turno.

Ver docs/SAFE-RENDERER.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.orchestrator.verificador.evidencia import URL_RE

SAFE_APPEND_KEY = "_safe_append"
IMAGE_URL_KEY = "_image_url"
TOOL_NAME_KEY = "_tool_name"


@dataclass
class SafeExtras:
    safe_appends: list[str] = field(default_factory=list)
    image_url: str | None = None


def extract_safe_extras(tool_results: list[dict[str, Any]]) -> SafeExtras:
    extras = SafeExtras()
    for result in tool_results:
        append = result.get(SAFE_APPEND_KEY)
        if isinstance(append, str) and append.strip():
            extras.safe_appends.append(append.strip())
        image = result.get(IMAGE_URL_KEY)
        if isinstance(image, str) and image.strip():
            extras.image_url = image.strip()
    return extras


def apply_safe_extras(response_text: str, extras: SafeExtras, *, por_enlace: bool = False) -> str:
    """El texto con los appends de las tools al final, sin repetir.

    `por_enlace` (solo con el motor de procedimientos activo, adenda 2026-10-13 de ADR-0113): si el
    texto del modelo ya trae el link de un append, se quitan del texto las líneas con ese link y el
    append del core queda (el cliente ve el link una sola vez, con el texto exacto de la tool). Un
    append sin URL (los datos de una transferencia) se agrega como siempre."""
    if not extras.safe_appends:
        return response_text
    if por_enlace:
        response_text = _sin_lineas_con_enlaces(response_text, extras.safe_appends)
    # Dedup: no repetir un append que el modelo ya escribió en su respuesta
    # (p. ej. copió el link del resultado de la tool) ni repetir appends
    # idénticos de varias tools en el mismo turno.
    seen: set[str] = set()
    pending: list[str] = []
    for append in extras.safe_appends:
        if append in seen or append in response_text:
            continue
        seen.add(append)
        pending.append(append)
    if not pending:
        return response_text
    texto = _sin_lineas_huerfanas(response_text, pending).rstrip()
    bloque = "\n\n".join(pending)
    return f"{texto}\n\n{bloque}" if texto else bloque


def _sin_lineas_con_enlaces(texto: str, appends: list[str]) -> str:
    """El texto sin las líneas que traen una URL de algún append (la línea del core la reemplaza)."""
    urls = {u.rstrip(_COLA_URL) for append in appends for u in URL_RE.findall(append)} - {""}
    if not urls:
        return texto
    lineas = [
        linea for linea in texto.splitlines()
        if not any(u.rstrip(_COLA_URL) in urls for u in URL_RE.findall(linea))
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


# Puntuación de la oración pegada al final de una URL ("…/abc." o "…/abc*").
_COLA_URL = ".,;:!?¡¿*_~"


# Viñetas y puntuación que no cambian una línea ("- 💳 Paga aquí:" es "💳 Paga aquí").
_BORDE_LINEA = " \t-*•·:.,;"


def _esqueleto(linea: str) -> str:
    """La línea sin sus URLs, con los espacios colapsados y sin viñetas ni puntuación final."""
    return re.sub(r"\s+", " ", URL_RE.sub(" ", linea)).strip(_BORDE_LINEA)


def _sin_lineas_huerfanas(texto: str, appends: list[str]) -> str:
    """Quita del texto las líneas sin URL que repiten una línea con URL de un append.

    El modelo imita el formato del append ("💳 Podés pagar en línea aquí: <url>") y el
    verificador quita la URL que no tiene respaldo (ADR-0119): quedaría la línea sin link y,
    debajo, la misma línea completa que agrega el core (visto en test el 2026-10-10)."""
    esqueletos = {
        _esqueleto(linea) for append in appends for linea in append.splitlines() if URL_RE.search(linea)
    } - {""}
    if not esqueletos:
        return texto
    lineas = [
        linea for linea in texto.splitlines() if URL_RE.search(linea) or _esqueleto(linea) not in esqueletos
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas))


def strip_reserved_keys(result: dict[str, Any]) -> dict[str, Any]:
    """Lo que ve el LLM: el resultado SIN las claves reservadas (no puede parafrasearlas)."""
    return {k: v for k, v in result.items() if k not in (SAFE_APPEND_KEY, IMAGE_URL_KEY, TOOL_NAME_KEY)}
