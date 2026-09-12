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

from dataclasses import dataclass, field
from typing import Any

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


def apply_safe_extras(response_text: str, extras: SafeExtras) -> str:
    if not extras.safe_appends:
        return response_text
    return response_text.rstrip() + "\n\n" + "\n\n".join(extras.safe_appends)


def strip_reserved_keys(result: dict[str, Any]) -> dict[str, Any]:
    """Lo que ve el LLM: el resultado SIN las claves reservadas (no puede parafrasearlas)."""
    return {k: v for k, v in result.items() if k not in (SAFE_APPEND_KEY, IMAGE_URL_KEY, TOOL_NAME_KEY)}
