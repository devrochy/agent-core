"""Proveedor Anthropic (Claude) vía Messages API con httpx.

Soporta prompt caching jerárquico: cada bloque de `system_blocks` recibe su
propio cache_control (ADR-021 de agent-sale — hasta 4 breakpoints).
Bloque 1: prompt base + tools (byte-idéntico entre turnos).
Bloque 2: tono (variantes fijas). Bloque 3: voz de marca. Queda 1 libre.

REGLA DE ORO: nunca interpolar fecha/hora u otros valores variables en los
bloques de system — invalida el cache silenciosamente (docs/PROMPT-CACHING-GUIDE.md).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from core.llm.base import BaseLLMProvider
from core.llm.types import (
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    LLMToolSchema,
    LLMUsageInfo,
    StopReason,
)
from core.shared.exceptions import LLMProviderError
from core.shared.logger import get_logger

log = get_logger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_RETRIES = 2

PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class AnthropicProvider(BaseLLMProvider):
    provider_name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.pricing_per_mtok = PRICING.get(model, (3.00, 15.00))

    # ------------------------------------------------------------ serialización
    @staticmethod
    def _serialize_system(system_blocks: list[str]) -> list[dict[str, Any]]:
        blocks = []
        for text in system_blocks:
            if not text:
                continue
            blocks.append({"type": "text", "text": text, "cache_control": {"type": "ephemeral"}})
        return blocks

    @staticmethod
    def _serialize_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )
            elif msg.role == "assistant" and msg.tool_calls:
                content: list[dict[str, Any]] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content.append(
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                    )
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": msg.role, "content": msg.content})
        return out

    @staticmethod
    def _serialize_tools(tools: list[LLMToolSchema]) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

    # ------------------------------------------------------------ generate
    async def generate(
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": self._serialize_system(system_blocks),
            "messages": self._serialize_messages(messages),
        }
        if tools:
            payload["tools"] = self._serialize_tools(tools)

        started = time.monotonic()
        data = await self._post_with_retries(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        text_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        for block in data.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(
                    LLMToolCall(id=block["id"], name=block["name"], arguments=block.get("input", {}))
                )

        raw_stop = data.get("stop_reason", "end_turn")
        stop_reason: StopReason
        if raw_stop == "tool_use":
            stop_reason = "tool_use"
        elif raw_stop == "max_tokens":
            stop_reason = "max_tokens"
        elif raw_stop == "refusal":
            stop_reason = "refusal"
        else:
            stop_reason = "end_turn"

        usage = data.get("usage", {})
        return LLMResponse(
            content="\n".join(text_parts),
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            usage=LLMUsageInfo(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            ),
            model=self.model,
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------ stream
    async def stream(
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": self._serialize_system(system_blocks),
            "messages": self._serialize_messages(messages),
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", API_URL, json=payload, headers=self._headers()
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {}).get("text")
                            if delta:
                                yield delta
                    except json.JSONDecodeError:
                        continue

    # ------------------------------------------------------------ http
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    async def _post_with_retries(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    response = await client.post(API_URL, json=payload, headers=self._headers())
                if response.status_code == 429 or response.status_code >= 500:
                    raise LLMProviderError(f"anthropic HTTP {response.status_code}: {response.text[:300]}")
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise LLMProviderError("Respuesta no-JSON de anthropic")
                return result
            except (httpx.HTTPError, LLMProviderError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1.5 * (2**attempt))
        raise LLMProviderError(f"anthropic agotó reintentos: {last_error}")
