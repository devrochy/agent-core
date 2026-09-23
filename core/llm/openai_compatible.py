"""Proveedor para APIs compatibles con OpenAI Chat Completions.

Cubre: DeepSeek (default del framework), OpenAI, xAI, Groq, Together, etc.
Sin SDK: httpx nativo (mismo criterio que agent-sale ADR-010 — menos deps,
control total del payload).

DeepSeek:
  - deepseek-chat     -> V3, function calling nativo, económico. Default.
  - deepseek-reasoner -> R1, razonamiento profundo. NO soporta function calling:
    el difficulty_router solo lo usa en turnos sin tools.
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

MAX_RETRIES = 2
RETRY_BACKOFF_S = 1.5

# USD por millón de tokens (input, output) — actualizar si cambia el pricing.
PRICING: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}


class OpenAICompatibleProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str, base_url: str, provider_name: str = "openai_compatible") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.provider_name = provider_name
        self.pricing_per_mtok = PRICING.get(model, (0.0, 0.0))

    # ------------------------------------------------------------ serialización
    def _serialize_messages(self, system_blocks: list[str], messages: list[LLMMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # Los bloques de system se concatenan (DeepSeek no tiene prompt caching por bloque;
        # su context caching es automático por prefijo — mantener los bloques estables lo aprovecha).
        if system_blocks:
            out.append({"role": "system", "content": "\n\n".join(b for b in system_blocks if b)})
        for msg in messages:
            if msg.role == "tool":
                out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
            elif msg.role == "assistant" and msg.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": msg.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
            else:
                out.append({"role": msg.role, "content": msg.content})
        return out

    @staticmethod
    def _serialize_tools(tools: list[LLMToolSchema]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in tools
        ]

    # ------------------------------------------------------------ generate
    async def generate(
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(system_blocks, messages),
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = self._serialize_tools(tools)
            payload["tool_choice"] = "auto"

        started = time.monotonic()
        data = await self._post_with_retries("/chat/completions", payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        try:
            choice = data["choices"][0]
            message = choice["message"]
            finish = choice.get("finish_reason", "stop")
        except (KeyError, IndexError) as exc:
            raise LLMProviderError(f"Respuesta inesperada de {self.provider_name}: {data}") from exc

        tool_calls: list[LLMToolCall] = []
        for tc in message.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(LLMToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args))

        stop_reason: StopReason
        if tool_calls:
            stop_reason = "tool_use"
        elif finish == "length":
            stop_reason = "max_tokens"
        elif finish == "content_filter":
            stop_reason = "refusal"
        else:
            stop_reason = "end_turn"

        usage = data.get("usage", {})
        return LLMResponse(
            content=message.get("content") or "",
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            usage=LLMUsageInfo(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cache_read_tokens=usage.get("prompt_cache_hit_tokens", 0),
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
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize_messages(system_blocks, messages),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0]["delta"].get("content")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    # ------------------------------------------------------------ http
    async def _post_with_retries(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    response = await client.post(
                        f"{self.base_url}{path}",
                        json=payload,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    raise LLMProviderError(
                        f"{self.provider_name} HTTP {response.status_code}: {response.text[:300]}"
                    )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise LLMProviderError(f"Respuesta no-JSON de {self.provider_name}")
                return result
            except (httpx.HTTPError, LLMProviderError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_S * (2**attempt)
                    log.warning("llm_retry", provider=self.provider_name, attempt=attempt + 1, wait_s=wait)
                    await asyncio.sleep(wait)
        raise LLMProviderError(f"{self.provider_name} agotó reintentos: {last_error}")
