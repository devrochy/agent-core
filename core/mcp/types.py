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
from typing import TYPE_CHECKING, Any, ClassVar

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


def _is_private_host(host: str) -> bool:
    """True si el host es una IP interna/loopback/link-local o metadata de cloud.

    Bloquea el vector SSRF aunque la allowlist del tenant la incluya (ADR-0052).
    Los nombres que resuelven a IP privada quedan fuera de este chequeo (no se
    resuelve DNS); se documenta como limitación.
    """
    import ipaddress

    if host in ("localhost", "metadata.google.internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


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

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        # Nunca permitir direcciones internas/link-local/loopback ni metadata de
        # cloud (SSRF), aunque estuvieran en la allowlist (ADR-0052).
        if _is_private_host(host):
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
    # Tipos de conexión activos que la tool necesita para ofrecerse/ejecutarse
    # (ADR-0072; ej. ["google_calendar"]). Ortogonal a packs/salud/scopes.
    requires_connection_types: list[str] = field(default_factory=list)
    # Gating condicional (ADR-0084): si se define, decide POR REQUEST si la tool
    # puede ofrecerse, recibiendo los tipos de conexión activos del tenant. Se usa
    # cuando el requisito depende de la config del pack (p. ej. pack-calendar en
    # modo `interno` no necesita Google; en modo `google` sí). Tiene prioridad
    # sobre `requires_connection_types`.
    connection_predicate: Callable[[set[str]], Awaitable[bool]] | None = None


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

    # --- Catálogo de habilidades (HU-FWK-04, SPEC-16) ---
    # Nombre visible al cliente (glosario: "habilidad", no "plugin"/"pack"). Si
    # falta, el catálogo cae a `name`.
    display_name: str = ""
    # Categoría del catálogo (p. ej. "Ventas", "Cobranza", "Agenda").
    category: str = ""
    # Conexiones/tipos que la habilidad necesita para funcionar (ADR-0072), ej.
    # ["whatsapp_meta", "google_calendar"]. Se muestran como requisitos.
    required_connections: ClassVar[list[str]] = []
    # Plan mínimo en el que está disponible (código de `core/billing/plans.py`),
    # ej. "free" | "basic" | "premium" | "empresarial".
    min_plan: str = "free"
    # Resumen de lo que hace, en una frase por acción (para la tarjeta del catálogo).
    action_summary: ClassVar[list[str]] = []
    # Guía de configuración breve (markdown corto). La guía extensa vive en
    # `TENANTS_ROOT`/playbooks (HU-UX-05).
    setup_guide: str = ""

    # Rutas del panel admin que el plugin aporta (aparecen en el menú lateral al
    # activarse). Claves opcionales (ADR-0061):
    #   - `section` (str): etiqueta de agrupación dentro del grupo del pack
    #     ("General", "Monitoreo", "Seguridad"); se renderiza preservando el orden.
    #   - `master_only` (bool): el item solo lo ve un admin `master`.
    admin_menu: list[dict[str, Any]] = []
    # [{"label": "Leads", "path": "/crm/leads", "section": "General"}]

    # Roles de colaborador que el pack aporta al alta de cuentas del panel
    # (ADR-0066). Se ofrecen en `/admin/colaboradores` solo si el pack está
    # activo. El pack recibe el aviso vía `on_collaborator_role` para crear su
    # perfil propio. Formato: [{"id": "gestor", "label": "Gestor de cobranza"}].
    collaborator_roles: list[dict[str, Any]] = []

    async def on_collaborator_role(self, tenant_id: uuid.UUID, admin_id: uuid.UUID, role_id: str) -> None:
        """Hook opcional (ADR-0066): el admin `admin_id` recibió el rol de pack
        `role_id` en `tenant_id`. El pack crea/reconcilia el perfil que lo marca
        como su usuario (ej. perfil `Gestor`). Default: no-op.
        """
        return None

    async def seed_data(self, tenant_id: uuid.UUID, data: dict[str, Any]) -> list[str]:
        """Hook opcional de seed (ADR-0083, issue #154): importa `data` a la BD
        del tenant de forma idempotente. El CLI/onboarding lo invoca para CADA
        pack habilitado que tenga un archivo `TENANTS_ROOT/<slug>/<plugin_id>.json`.
        Default: no-op.
        """
        return []

    def public_routes(self) -> list[Any]:
        """Routers PÚBLICOS del pack (sin sesión de admin), ej. la app de campo.

        El prefijo DEBE empezar por `/api/pack/` (ADR-0057): el core lo añade a la
        allowlist del middleware y el pack autentica cada request con su propia
        identidad. Default: ninguno.
        """
        return []

    def admin_api_routes(self) -> list[Any]:
        """Routers de API ADMIN del pack (ADR-0079), montados en `/api/v1`.

        El prefijo DEBE empezar por `/api/v1/pack/<plugin_id>`. El pack usa las
        dependencias del core (`core.api.deps.require`) para heredar la identidad
        de admin, el `X-Tenant-Id` validado y el RBAC por sección; el core solo
        monta. Default: ninguno.
        """
        return []

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
