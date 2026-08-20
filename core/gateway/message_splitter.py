"""Message Splitter: divide la respuesta del LLM en burbujas de WhatsApp.

Un solo mensaje largo es mala UX en chat. El system prompt le pide al LLM
separar ideas con doble salto de línea; este módulo consume esa estructura
(post-procesamiento puro, portado de agent-sale/messageSplitter.ts).

Estilos:
  un_mensaje    -> 1 burbuja
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


def split_for_bubbles(text: str, style: str = "pocos_cortos") -> list[str]:
    text = text.strip()
    if not text:
        return []
    if style == "un_mensaje":
        return [text]

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
