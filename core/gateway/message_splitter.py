"""Message Splitter: divide la respuesta del LLM en burbujas de WhatsApp.

Desde 2026-10-01 Meta cobra cada mensaje de servicio, así que por defecto se
envía **un solo mensaje** (`un_mensaje`). El negocio puede cambiarlo en
Configuración (HU-ARCH-11).

Estilos:
  un_mensaje    -> 1 burbuja (se parte solo por el límite técnico de WhatsApp)
  pocos_cortos  -> split por párrafo, máx 3
  varios_cortos -> split por párrafo y oración, máx 6

Nunca se descarta contenido: si hay más trozos que el límite, los últimos
se fusionan en la burbuja final.
"""

from __future__ import annotations

import re

PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

CAPS = {"un_mensaje": 1, "pocos_cortos": 3, "varios_cortos": 6}

# Límite técnico de la Cloud API de WhatsApp para el `body` de un texto.
WA_MAX_TEXT_LENGTH = 4096


def _split_by_length(text: str, limit: int = WA_MAX_TEXT_LENGTH) -> list[str]:
    """Parte por longitud real, respetando palabras y saltos de línea."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def split_for_bubbles(
    text: str, style: str = "un_mensaje", *, max_length: int = WA_MAX_TEXT_LENGTH
) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if style == "un_mensaje":
        # Un solo mensaje salvo que supere el límite técnico de WhatsApp.
        return _split_by_length(text, max_length)

    paragraphs = [p.strip() for p in PARAGRAPH_SPLIT.split(text) if p.strip()]

    if style == "varios_cortos":
        chunks: list[str] = []
        for paragraph in paragraphs:
            chunks.extend(s.strip() for s in SENTENCE_SPLIT.split(paragraph) if s.strip())
    else:
        chunks = paragraphs

    return _cap_chunks(chunks, CAPS.get(style, 3))


def _cap_chunks(chunks: list[str], max_chunks: int) -> list[str]:
    if len(chunks) <= max_chunks:
        return chunks
    head = chunks[: max_chunks - 1]
    tail = "\n\n".join(chunks[max_chunks - 1 :])
    return [*head, tail]
