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
    # Razonamiento de DeepSeek con thinking (HU-AOS-09): con tools hay que devolverlo en las
    # vueltas siguientes del turno. Vacío = no se envía (todo turno sin la política de riesgo).
    reasoning_content: str = ""


class BloqueDelTurno(str):
    """El bloque dinámico del turno (último de `system_blocks`), marcado para viajar detrás del
    historial: justo antes de `messages[ancla]`, el mensaje del cliente de este turno.

    Lleva la fecha y hora al minuto, la tarjeta y las memorias: cambia en cada turno. Delante del
    historial, como antes, cortaba la caché por prefijo del proveedor justo antes del historial;
    detrás, el historial de turnos anteriores entra al prefijo cacheado. Es un `str` para que los
    llamadores y los dobles de prueba que leen `system_blocks` sigan funcionando igual; un
    proveedor que no lo mueve (Anthropic) lo trata como el último bloque de sistema, como siempre.
    """

    ancla: int

    def __new__(cls, texto: str, ancla: int) -> BloqueDelTurno:
        bloque = super().__new__(cls, texto)
        bloque.ancla = ancla
        return bloque


def separar_bloque_del_turno(system_blocks: list[str]) -> tuple[list[str], BloqueDelTurno | None]:
    """Los bloques fijos y el bloque del turno, si el último lo es."""
    if system_blocks and isinstance(system_blocks[-1], BloqueDelTurno):
        return list(system_blocks[:-1]), system_blocks[-1]
    return list(system_blocks), None


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
    # Tokens de razonamiento (DeepSeek con thinking; ya incluidos en `output_tokens`).
    reasoning_tokens: int = 0
    # Si `input_tokens` ya incluye lo leído y lo escrito en caché (OpenAI-compatible sí,
    # Anthropic no). Viaja en cada respuesta: el respaldo de un turno puede ser de otro proveedor.
    # Lo usa el ledger para el total de entrada (adenda HU-AOS-35 de ADR-0117). El costo lo calcula
    # el proveedor con su propia regla (`input_includes_cache` del proveedor, ADR-0208 §13).
    input_includes_cache: bool = True


@dataclass
class LLMResponse:
    content: str
    stop_reason: StopReason
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    usage: LLMUsageInfo = field(default_factory=LLMUsageInfo)
    model: str = ""
    latency_ms: int = 0
    # `reasoning_content` del proveedor (DeepSeek con thinking); vacío en los demás casos.
    reasoning_content: str = ""
