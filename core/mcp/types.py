"""Tipos del MCP Host interno: el contrato entre plugins y core.

Conceptos (ver docs/PLUGIN-CONTRACT.md):

- MCPTool: una herramienta ejecutable, descrita con JSON Schema (estándar MCP).
  El core la traduce al formato de function calling del LLM activo.

- BaseSkill: agrupación SEMÁNTICA de tools. El SkillRouter inyecta en el
  system prompt solo las skills relevantes al contexto del turno.

- BaseModule: el plugin. Declara skills, guardrails, suscripciones a eventos
  y su schema de configuración (Pydantic -> formulario auto-generado en el admin).

- GuardrailResult: veredicto de una validación post-LLM.

Todo corre EN EL MISMO PROCESO. Ejecutar una tool es una llamada de función
Python, no una petición de red (docs/MONOLITH-MODEL.md).
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

# handler(arguments, context) -> resultado JSON-serializable
ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]]
GuardrailFn = Callable[[str, list[dict[str, Any]]], Awaitable["GuardrailResult"]]


@dataclass
class ToolContext:
    """Contexto inyectado por el core al ejecutar una tool.

    El LLM NUNCA ve estos valores: el orchestrator los inyecta
    (mismo principio que agent-sale: conversation_id/idempotency_key son del core).

    `http` (ADR-0052) es la única salida a red que una tool puede usar: acotada a
    una allowlist de dominios. Nunca se entrega el pool de Postgres ni `environ`.
    """

    conversation_id: uuid.UUID
    customer_id: uuid.UUID
    idempotency_key: str
    channel: str
    http: ScopedHttp | None = None


class ScopedHttp:
    """Cliente HTTP acotado a una allowlist de dominios (ADR-0052).

    Solo permite `POST`/`GET` a hosts de la allowlist. Sin allowlist, toda salida
    se rechaza (fail-closed). Nunca expone `httpx` crudo ni el entorno.
    """

    def __init__(self, allowed_domains: list[str] | None, timeout: float = 15.0) -> None:
        self._allowed = {d.strip().lower() for d in (allowed_domains or []) if d.strip()}
        self._timeout = timeout

    def _allowed_host(self, url: str) -> bool:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self._allowed)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if method.upper() not in ("GET", "POST"):
            return {"error": f"Método no permitido: {method}"}
        if not self._allowed_host(url):
            return {"error": f"Dominio no autorizado: {url}"}
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    method.upper(), url, params=params, json=json_body, headers=headers or {}
                )
        except Exception as exc:
            return {"error": f"Fallo de red: {type(exc).__name__}"}
        try:
            data = resp.json()
        except Exception:
            data = resp.text[:2000]
        return {"status": resp.status_code, "data": data}


@dataclass
class MCPTool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema: {"type": "object", "properties": {...}, "required": [...]}
    handler: ToolHandler
    plugin_id: str = ""  # lo completa el MCPHost al registrar
    scopes: list[str] = field(default_factory=list)  # scopes que requiere (ADR-0019)


@dataclass
class MCPResource:
    """Recurso de solo lectura que un plugin expone (estándar MCP)."""

    uri: str
    name: str
    description: str
    reader: Callable[[], Awaitable[str]]
    plugin_id: str = ""


@dataclass
class GuardrailResult:
    passed: bool
    reason: str = ""
    retry_instruction: str = ""  # instrucción correctiva para el reintento del LLM


@dataclass
class Guardrail:
    name: str
    fn: GuardrailFn
    priority: int = 10  # menor = se evalúa primero
    plugin_id: str = ""


class BaseSkill(ABC):
    """Agrupación semántica de tools. Ej: LeadSkill agrupa crear_lead + calificar_lead."""

    name: str = "skill"
    description: str = ""
    # Palabras clave que el SkillRouter usa para decidir relevancia por turno.
    # Lista vacía = la skill siempre está activa.
    keywords: list[str] = []

    @abstractmethod
    def get_tools(self) -> list[MCPTool]: ...


class BasePluginConfig(BaseModel):
    """Base del schema de configuración de un plugin.

    El FormGenerator del admin convierte este modelo en un formulario HTML.
    Los campos SecretStr se cifran con Fernet antes de persistir.
    """


class BaseModule(ABC):
    """Contrato del plugin. Ciclo de vida: Install -> Detect -> Configure -> Activate -> Run."""

    id: str = ""             # "mod_crm_engine" — debe coincidir con el entry_point
    name: str = ""           # "CRM Engine"
    version: str = "0.1.0"
    description: str = ""
    requires_config: bool = True  # False = se activa sin formulario (ej. plugins internos)
    config_schema: type[BasePluginConfig] | None = None

    # Rutas del panel admin que el plugin aporta (aparecen en el menú lateral al activarse)
    admin_menu: list[dict[str, str]] = []
    # [{"label": "Leads", "path": "/crm/leads"}]

    def extra_admin_routers(self) -> list[Any]:
        """Routers admin adicionales del pack (ej. pestañas de Configuración).

        Además del `admin_router` principal, un pack puede aportar routers
        propios que el core incluye al arrancar (ADR-0049). El prefijo DEBE
        empezar por `/admin` para quedar bajo el gate del panel; el core ignora
        (con warning) cualquiera que no lo cumpla. Default: ninguno.
        """
        return []

    def __init__(self) -> None:
        self.config: BasePluginConfig | None = None

    @abstractmethod
    def on_activate(self, host: MCPHost) -> None:
        """Registra skills, guardrails y suscripciones. Llamado al activar el plugin."""

    def on_deactivate(self) -> None:
        """Limpieza opcional al desactivar. El MCPHost retira los registros automáticamente."""


@dataclass
class SkillInfo:
    """Metadata de una skill registrada (para el panel admin y el SDD)."""

    name: str
    description: str
    plugin_id: str
    tools: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
