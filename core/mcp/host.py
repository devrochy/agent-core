"""MCPHost: registro y despacho in-process de tools, skills, guardrails y recursos.

El core actúa como Host MCP interno:
- Los plugins registran sus capacidades aquí (on_activate).
- El orchestrator pide las tools relevantes y las ejecuta a través del host.
- Ejecutar una tool = llamada de función Python en el mismo proceso (cero red).

Manejo de errores: si una tool lanza una excepción, el host la reporta como
PluginError. El plugin_manager desactiva el plugin automáticamente y el
asistente sigue funcionando (docs/PLUGIN-CONTRACT.md § Errores).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.mcp.types import (
    BaseSkill,
    Guardrail,
    GuardrailFn,
    MCPResource,
    MCPTool,
    ScopedHttp,
    SkillInfo,
    ToolContext,
)
from core.shared.events import ON_TOOL_EXECUTED, EventHandler, event_bus
from core.shared.exceptions import BusinessError, PluginError
from core.shared.logger import get_logger

log = get_logger(__name__)


class MCPHost:
    def __init__(self) -> None:
        self._tools: dict[str, MCPTool] = {}
        self._skills: dict[str, tuple[BaseSkill, str]] = {}  # name -> (skill, plugin_id)
        self._guardrails: list[Guardrail] = []
        self._resources: dict[str, MCPResource] = {}
        self._plugin_handlers: dict[str, list[EventHandler]] = {}
        self._active_plugin: str = ""  # plugin en proceso de activación

    # ------------------------------------------------------------ registro (llamado por plugins)
    def begin_activation(self, plugin_id: str) -> None:
        self._active_plugin = plugin_id
        self._plugin_handlers.setdefault(plugin_id, [])

    def end_activation(self) -> None:
        self._active_plugin = ""

    def register_skill(self, skill: BaseSkill) -> None:
        plugin_id = self._active_plugin
        if skill.name in self._skills:
            raise PluginError(plugin_id, f"Skill duplicada: {skill.name}")
        self._skills[skill.name] = (skill, plugin_id)
        for tool in skill.get_tools():
            if tool.name in self._tools:
                raise PluginError(plugin_id, f"Tool duplicada: {tool.name}")
            tool.plugin_id = plugin_id
            self._tools[tool.name] = tool
        log.info("skill_registered", skill=skill.name, plugin=plugin_id,
                 tools=[t.name for t in skill.get_tools()])

    def register_guardrail(self, name: str, fn: GuardrailFn, priority: int = 10) -> None:
        # Unicidad de `name`, mismo criterio que `register_skill`/tools de
        # arriba: el toggle individual por tenant (ADR de parametrización,
        # `AppSettings.behavior_config["disabled_guardrails"]`) filtra por
        # NOMBRE -- si dos packs pudieran registrar el mismo nombre, apagar
        # uno apagaría el del otro pack sin que el admin se entere
        # (hallazgo de `auditor-multitenant`).
        if any(g.name == name for g in self._guardrails):
            raise PluginError(self._active_plugin, f"Guardrail duplicado: {name}")
        self._guardrails.append(
            Guardrail(name=name, fn=fn, priority=priority, plugin_id=self._active_plugin)
        )
        self._guardrails.sort(key=lambda g: g.priority)

    def register_resource(self, resource: MCPResource) -> None:
        resource.plugin_id = self._active_plugin
        self._resources[resource.uri] = resource

    def subscribe(self, event: str, handler: EventHandler) -> None:
        """Suscripción a eventos del core con tracking por plugin (para limpieza al desactivar)."""
        event_bus.subscribe(event, handler)
        self._plugin_handlers.setdefault(self._active_plugin, []).append(handler)

    async def trigger_escalation(self, context: ToolContext, reason: str, summary: str = "") -> None:
        """API para que un plugin dispare el handoff nativo del core."""
        from core.orchestrator.escalation import escalate

        await escalate(context.conversation_id, reason=f"plugin:{reason}", summary=summary)

    # ------------------------------------------------------------ desregistro
    def remove_plugin(self, plugin_id: str) -> None:
        self._tools = {k: v for k, v in self._tools.items() if v.plugin_id != plugin_id}
        self._skills = {k: v for k, v in self._skills.items() if v[1] != plugin_id}
        self._guardrails = [g for g in self._guardrails if g.plugin_id != plugin_id]
        self._resources = {k: v for k, v in self._resources.items() if v.plugin_id != plugin_id}
        handlers = self._plugin_handlers.pop(plugin_id, [])
        event_bus.unsubscribe_all(handlers)
        log.info("plugin_capabilities_removed", plugin=plugin_id)

    # ------------------------------------------------------------ consulta (llamado por el core)
    def list_skills(self) -> list[SkillInfo]:
        return [
            SkillInfo(
                name=skill.name,
                description=skill.description,
                plugin_id=plugin_id,
                tools=[t.name for t in skill.get_tools()],
                keywords=list(skill.keywords),
            )
            for skill, plugin_id in self._skills.values()
        ]

    def get_skill_tools(self, skill_names: list[str]) -> list[MCPTool]:
        tools: list[MCPTool] = []
        for name in skill_names:
            entry = self._skills.get(name)
            if entry:
                skill, _plugin = entry
                tools.extend(self._tools[t.name] for t in skill.get_tools() if t.name in self._tools)
        return tools

    def get_tool(self, tool_name: str) -> MCPTool | None:
        """Devuelve la tool registrada por nombre, o None. El plugin_id permite
        aplicar el enablement por tenant en la costura de ejecución (ADR-0019)."""
        return self._tools.get(tool_name)

    def get_tools(self) -> list[MCPTool]:
        """Todas las tools registradas (para la pestaña Habilidades, ADR-0050)."""
        return list(self._tools.values())

    def get_guardrails(self) -> list[Guardrail]:
        return list(self._guardrails)

    # ------------------------------------------------------------ ejecución
    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        tool = self._tools.get(tool_name)
        if tool is None:
            return {"error": f"Herramienta desconocida: {tool_name}"}

        try:
            result = await tool.handler(arguments, context)
        except BusinessError as exc:
            # Rechazo de negocio (HU-ARCH-18): resultado controlado, el pack
            # SIGUE habilitado. Nunca se convierte en PluginError.
            result = exc.as_result()
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError(tool.plugin_id, f"Tool '{tool_name}' falló: {exc}") from exc

        await event_bus.emit(
            ON_TOOL_EXECUTED,
            tool_name=tool_name,
            plugin_id=tool.plugin_id,
            conversation_id=context.conversation_id,
        )
        return result

    @staticmethod
    def serialize_result(result: dict[str, Any]) -> str:
        return json.dumps(result, ensure_ascii=False, default=str)


mcp_host = MCPHost()


def new_tool_context(
    conversation_id: uuid.UUID,
    customer_id: uuid.UUID,
    idempotency_key: str,
    channel: str,
    http: ScopedHttp | None = None,
) -> ToolContext:
    return ToolContext(
        conversation_id=conversation_id,
        customer_id=customer_id,
        idempotency_key=idempotency_key,
        channel=channel,
        http=http,
    )
