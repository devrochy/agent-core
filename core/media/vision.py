"""Dispatcher de visión para imágenes entrantes (SPEC-04, HU-MEDIA-04; ADR-0151).

Un único punto de contacto con la red por proveedor. No interpreta el dominio:
devuelve el texto crudo y los tokens; quien llama decide si es una descripción
válida. Anthropic habla su API de mensajes; DeepSeek y OpenAI comparten
`/chat/completions` con `image_url` en data URI.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from core.config import get_settings
from core.media.catalog import VISION_CATALOG, vision_cost_usd
from core.media.errors import MediaProviderError, error_from_status

# PNG 1x1 blanco: la prueba de conexión no necesita una foto real.
PRUEBA_IMAGEN_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True, slots=True)
class VisionResult:
    text: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    provider: str
    model: str


async def _anthropic(
    base_url: str, api_key: str, model: str, contenido: bytes, mime_type: str, prompt: str, max_tokens: int
) -> tuple[str | None, int, int]:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": base64.b64encode(contenido).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    async with httpx.AsyncClient(timeout=get_settings().vision_timeout_s) as client:
        response = await client.post(
            f"{base_url}/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
    if response.status_code >= 400:
        raise error_from_status(response.status_code, response.text)
    data = response.json()
    blocks = data.get("content") or []
    text = next((b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"), None)
    usage = data.get("usage") or {}
    return text, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


async def _openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    contenido: bytes,
    mime_type: str,
    prompt: str,
    max_tokens: int,
    *,
    disable_thinking: bool,
) -> tuple[str | None, int, int]:
    body: dict[str, object] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{base64.b64encode(contenido).decode()}"
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    if disable_thinking:
        # DeepSeek V4 enciende razonamiento por defecto y consume el mismo
        # `max_tokens`: con presupuestos cortos se queda sin escribir la respuesta.
        body["thinking"] = {"type": "disabled"}
    async with httpx.AsyncClient(timeout=get_settings().vision_timeout_s) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
        )
    if response.status_code >= 400:
        raise error_from_status(response.status_code, response.text)
    data = response.json()
    choices = data.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    usage = data.get("usage") or {}
    return (
        content if isinstance(content, str) else None,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
    )


async def describir_imagen(
    contenido: bytes,
    mime_type: str,
    *,
    provider: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int = 120,
) -> VisionResult:
    """Llama al proveedor y devuelve el texto crudo + tokens + costo estimado."""
    if provider not in VISION_CATALOG:
        raise MediaProviderError(f"Proveedor de visión desconocido: {provider}", code="vision_provider_invalido")
    entry = VISION_CATALOG[provider]  # type: ignore[index]
    if not api_key:
        raise MediaProviderError("Falta la llave del proveedor de visión.", code="vision_sin_llave")
    mime = (mime_type or "image/jpeg").split(";")[0].strip().lower()
    try:
        if entry.family == "anthropic":
            text, tokens_in, tokens_out = await _anthropic(
                entry.base_url, api_key, model, contenido, mime, prompt, max_tokens
            )
        else:
            text, tokens_in, tokens_out = await _openai_compatible(
                entry.base_url,
                api_key,
                model,
                contenido,
                mime,
                prompt,
                max_tokens,
                disable_thinking=provider == "deepseek",
            )
    except httpx.TimeoutException as exc:
        raise MediaProviderError(
            "El proveedor de visión no respondió a tiempo.", transient=True, code="vision_timeout"
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaProviderError(
            f"No se pudo conectar con el proveedor de visión: {type(exc).__name__}",
            transient=True,
            code="vision_red",
        ) from exc
    return VisionResult(
        text=text.strip() if isinstance(text, str) and text.strip() else None,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=vision_cost_usd(provider, model, tokens_in, tokens_out),  # type: ignore[arg-type]
        provider=provider,
        model=model,
    )


__all__ = ["PRUEBA_IMAGEN_PNG", "VisionResult", "describir_imagen"]
