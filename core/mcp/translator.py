"""MCP Translator: convierte MCPTools al formato de function calling del LLM activo.

Los plugins describen sus tools UNA vez (JSON Schema estándar MCP).
El translator las serializa al contrato neutro LLMToolSchema; cada
provider (openai_compatible / anthropic) hace la serialización final.
"""

from __future__ import annotations

from core.llm.types import LLMToolSchema
from core.mcp.types import MCPTool


def to_llm_schemas(tools: list[MCPTool]) -> list[LLMToolSchema]:
    return [
        LLMToolSchema(name=t.name, description=t.description, parameters=t.parameters)
        for t in tools
    ]


def skills_summary(tools: list[MCPTool]) -> str:
    """Los nombres de las tools activas, para el bloque dinámico del system prompt. Sin descripción:
    la del esquema de funciones basta y repetirla costaba ~1k tokens sin caché por llamada."""
    if not tools:
        return ""
    return "Puedes usar estas herramientas: " + ", ".join(t.name for t in tools) + "."
