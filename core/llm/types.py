"""Contrato neutro entre el orchestrator y cualquier proveedor LLM.

Equivalente al `types.ts` de agent-sale (ADR-010): el orchestrator arma
LLMMessage[] y recibe LLMResponse, sin conocer el formato del proveedor.

IMPORTANTE: `system_blocks` es list[str], no str (ADR-021 de agent-sale).
Cada bloque puede recibir cache_control independiente en proveedores que
soporten prompt caching (Anthropic). En DeepSeek se concatenan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMMessage:
    role: Role
    content: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # para role="tool" (resultado de una tool)


@dataclass
class LLMToolSchema:
    """Schema neutro de una tool (el MCP Translator lo genera desde MCPTool)."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class LLMUsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LLMResponse:
    content: str
    stop_reason: StopReason
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    usage: LLMUsageInfo = field(default_factory=LLMUsageInfo)
    model: str = ""
    latency_ms: int = 0
