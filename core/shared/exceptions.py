"""Jerarquía de excepciones del core.

Regla de manejo de errores (ver docs/PLUGIN-CONTRACT.md):
- PluginError lanzada durante ejecución de una tool → el core la captura,
  desactiva el plugin automáticamente y notifica al panel admin. El asistente sigue vivo.
- LLMProviderError → reintento con backoff; si persiste, respuesta fallback + escalación.
"""

from __future__ import annotations


class AgentCoreError(Exception):
    """Base de todas las excepciones del framework."""


class PluginError(AgentCoreError):
    """Error dentro de un plugin. El core lo aísla: nunca tumba el asistente."""

    def __init__(self, plugin_id: str, message: str) -> None:
        self.plugin_id = plugin_id
        super().__init__(f"[{plugin_id}] {message}")


class PluginConfigError(PluginError):
    """La configuración del plugin es inválida o está incompleta."""


class LLMProviderError(AgentCoreError):
    """Fallo de comunicación o respuesta inválida del proveedor LLM."""


class ChannelError(AgentCoreError):
    """Fallo en un adaptador de canal (envío o parseo)."""


class MemoryError_(AgentCoreError):
    """Fallo en una capa de memoria. Working/Vector degradan con gracia; Entity no."""


class GuardrailViolation(AgentCoreError):
    """Un guardrail bloqueó la respuesta del LLM."""

    def __init__(self, guardrail_name: str, reason: str, retry_instruction: str = "") -> None:
        self.guardrail_name = guardrail_name
        self.reason = reason
        self.retry_instruction = retry_instruction
        super().__init__(f"Guardrail '{guardrail_name}': {reason}")
