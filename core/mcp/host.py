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
from collections.abc import Mapping
from typing import Any

from agentcore_contracts import Procedure

from core.mcp.types import (
    AfirmacionesFn,
    BaseSkill,
    CandidatosFn,
    Guardrail,
    GuardrailFn,
    MCPResource,
    MCPTool,
    ReferenciasFn,
    Resolver,
    ResumenFn,
    ScopedHttp,
    SkillInfo,
    ToolContext,
    ValidarDatoFn,
)
from core.shared.events import ON_TOOL_EXECUTED, EventHandler, event_bus
from core.shared.exceptions import BusinessError, PluginError
from core.shared.logger import get_logger

log = get_logger(__name__)

# Tools del core que despacha un pack: prefijo → pack dueño. Solo ese pack puede registrar su resumen de confirmación.
TOOLS_DEL_CORE_POR_PACK = {"sub_": "pack_subscriptions"}


class MCPHost:
    def __init__(self) -> None:
        self._tools: dict[str, MCPTool] = {}
        self._skills: dict[str, tuple[BaseSkill, str]] = {}  # name -> (skill, plugin_id)
        self._guardrails: list[Guardrail] = []
        # Fuentes de afirmaciones de cierre por plugin (ADR-0119): el verificador
        # del core las pide en cada turno, solo de los packs habilitados.
        self._afirmaciones: dict[str, AfirmacionesFn] = {}
        # Fuentes de referencias vigentes por plugin (ADR-0130): el loop las pide en cada
        # turno, solo de los packs habilitados y sanos para el negocio.
        self._referencias: dict[str, ReferenciasFn] = {}
        # Procedimientos (`procedure/1`) y *resolvers* por plugin (HU-AOS-05): el motor del
        # kernel los lee (`core/kernel/procedures/registro.py`), solo de los packs habilitados.
        self._procedimientos: dict[str, list[Procedure]] = {}
        self._resolvers: dict[str, Resolver] = {}
        # Resúmenes de confirmación por acción (HU-AOS-07): acción -> (fn, plugin_id).
        self._resumenes: dict[str, tuple[ResumenFn, str]] = {}
        # Rótulos humanos de los datos de los procedimientos de cada plugin (adenda 2026-10-13).
        self._rotulos: dict[str, dict[str, str]] = {}
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

    def register_guardrail(
        self,
        name: str,
        fn: GuardrailFn,
        priority: int = 10,
        *,
        etiqueta: str = "",
        explicacion: str = "",
    ) -> None:
        # Unicidad de `name`, mismo criterio que `register_skill`/tools de
        # arriba: el toggle individual por tenant (ADR de parametrización,
        # `AppSettings.behavior_config["disabled_guardrails"]`) filtra por
        # NOMBRE -- si dos packs pudieran registrar el mismo nombre, apagar
        # uno apagaría el del otro pack sin que el admin se entere
        # (hallazgo de `auditor-multitenant`).
        if any(g.name == name for g in self._guardrails):
            raise PluginError(self._active_plugin, f"Guardrail duplicado: {name}")
        # Los interruptores del verificador del core (y los nombres viejos que traducen)
        # son reservados: apagar uno de un pack apagaría el verificador (ADR-0119).
        from core.orchestrator.verificador.verificador import ALIAS, TOGGLES

        if name in TOGGLES or name in ALIAS:
            raise PluginError(self._active_plugin, f"Nombre de guardrail reservado: {name}")
        self._guardrails.append(
            Guardrail(
                name=name,
                fn=fn,
                priority=priority,
                plugin_id=self._active_plugin,
                etiqueta=etiqueta,
                explicacion=explicacion,
            )
        )
        self._guardrails.sort(key=lambda g: g.priority)

    def register_afirmaciones(self, fn: AfirmacionesFn) -> None:
        """Registra las afirmaciones de cierre del plugin que se activa (ADR-0119).

        `fn` se llama en cada turno dentro del `tenant_scope` del negocio, así que
        puede leer la config del pack (claims desactivados y propios, ADR-0025).
        Una por plugin."""
        plugin_id = self._active_plugin
        if plugin_id in self._afirmaciones:
            raise PluginError(plugin_id, "Afirmaciones ya registradas")
        self._afirmaciones[plugin_id] = fn

    def get_afirmaciones(self) -> dict[str, AfirmacionesFn]:
        """Fuentes de afirmaciones por `plugin_id` (el loop filtra por packs habilitados)."""
        return dict(self._afirmaciones)

    def register_referencias(self, fn: ReferenciasFn) -> None:
        """Registra la fuente de referencias vigentes del plugin que se activa (ADR-0130).

        `fn(conversation_id, customer_id)` se llama en cada turno dentro del
        `tenant_scope` del negocio. Una por plugin."""
        plugin_id = self._active_plugin
        if plugin_id in self._referencias:
            raise PluginError(plugin_id, "Referencias ya registradas")
        self._referencias[plugin_id] = fn

    def get_referencias(self) -> dict[str, ReferenciasFn]:
        """Fuentes de referencias por `plugin_id` (el loop filtra por packs habilitados)."""
        return dict(self._referencias)

    def register_procedures(self, procedures: list[Procedure]) -> None:
        """Registra los procedimientos que publica el plugin que se activa (ADR-0113 §1).

        El dueño del id (`pack_subscriptions.contratar_plan`) debe ser el plugin. Un procedimiento
        ajeno o con un id repetido no se registra (log), y el plugin sigue vivo: un
        procedimiento mal escrito no puede dejar sin tools a todos los negocios del proceso.
        El linter completo (acciones del binario) corre en CI y, en runtime, al usarlo
        (`registro.py`). Una llamada por plugin."""
        plugin_id = self._active_plugin
        if plugin_id in self._procedimientos:
            raise PluginError(plugin_id, "Procedimientos ya registrados")
        propios: list[Procedure] = []
        vistos = {p.id for lista in self._procedimientos.values() for p in lista}
        for proc in procedures:
            if not isinstance(proc, Procedure) or proc.owner != plugin_id or proc.id in vistos:
                log.error(
                    "procedimiento_rechazado",
                    plugin=plugin_id,
                    procedure_id=getattr(proc, "id", None),
                )
                continue
            vistos.add(proc.id)
            propios.append(proc)
        self._procedimientos[plugin_id] = propios

    def get_procedures(self) -> dict[str, list[Procedure]]:
        """Procedimientos por `plugin_id` (el motor filtra por packs habilitados)."""
        return {k: list(v) for k, v in self._procedimientos.items()}

    def register_resolver(
        self,
        name: str,
        *,
        validar: ValidarDatoFn | None = None,
        candidatos: CandidatosFn | None = None,
    ) -> None:
        """Registra un *resolver* `<plugin>.<función>` del plugin que se activa (HU-AOS-05).
        Un procedimiento solo usa resolvers de su habilidad o del core (linter)."""
        plugin_id = self._active_plugin
        if not name.startswith(f"{plugin_id}.") or name in self._resolvers:
            raise PluginError(plugin_id, f"Resolver inválido o duplicado: {name}")
        if validar is None and candidatos is None:
            raise PluginError(plugin_id, f"Resolver sin funciones: {name}")
        self._resolvers[name] = Resolver(name=name, validar=validar, candidatos=candidatos, plugin_id=plugin_id)

    def get_resolver(self, name: str) -> Resolver | None:
        return self._resolvers.get(name)

    def register_resumen(self, accion: str, fn: ResumenFn) -> None:
        """Registra el resumen de confirmación de una acción del plugin que se activa (HU-AOS-07).

        Cuando el motor pide confirmar esa acción, el cliente ve lo que devuelve `fn` (leído del
        estado del pack, p. ej. el total vigente de la cotización) en vez de los argumentos
        crudos. Solo para una tool que el mismo plugin ya registró, o una del core que ese pack despacha
        (`TOOLS_DEL_CORE_POR_PACK`); una por acción."""
        plugin_id = self._active_plugin
        tool = self._tools.get(accion)
        # Una tool del core (`sub_*`, ADR-0106) también puede tener resumen del pack que la despacha: el pack
        # lee su estado; el kernel solo lo usa si ese pack está permitido en el turno. Solo el pack dueño del
        # prefijo: otro pack no puede reclamar el resumen de una tool del core que no es suya.
        propia = tool is not None and tool.plugin_id == plugin_id
        del_core = (
            tool is not None
            and tool.plugin_id == "core"
            and any(accion.startswith(pref) and plugin_id == dueno for pref, dueno in TOOLS_DEL_CORE_POR_PACK.items())
        )
        if not (propia or del_core) or accion in self._resumenes:
            raise PluginError(plugin_id, f"Resumen inválido o duplicado: {accion}")
        self._resumenes[accion] = (fn, plugin_id)

    def get_resumen(self, accion: str) -> tuple[ResumenFn, str] | None:
        """(fn, plugin_id) del resumen de una acción, o None (el motor filtra por packs permitidos)."""
        return self._resumenes.get(accion)

    def register_rotulos(self, rotulos: Mapping[str, str]) -> None:
        """Rótulos humanos de los datos de los procedimientos del plugin que se activa (adenda
        2026-10-13 de ADR-0113): `{"email": "Correo"}`, y de los valores de una lista:
        `{"payment_method.pago_en_linea": "Pago en línea"}`. Los usa el kernel en los mensajes que
        arma él (confirmación, pregunta de lo que falta) y en la tarjeta de estado; sin rótulo, el
        nombre del dato con espacios. Solo claves que son datos de un procedimiento que el mismo
        plugin ya registró; el texto se sanea a una línea de hasta 40 caracteres. Son datos de la
        habilidad: sin override por negocio y fuera de la release (ver la adenda)."""
        from core.kernel.confirmacion import limpiar

        plugin_id = self._active_plugin
        datos: set[str] = set()
        for proc in self._procedimientos.get(plugin_id, []):
            for n, slot in proc.slots.items():
                datos.add(n)
                datos.update(f"{n}.{v}" for v in (slot.enum or []))
        propios: dict[str, str] = {}
        for nombre, rotulo in rotulos.items():
            texto = limpiar(rotulo, 40) if isinstance(rotulo, str) else ""
            if nombre not in datos or not texto:
                raise PluginError(plugin_id, f"Rótulo inválido: {nombre}")
            propios[str(nombre)] = texto
        self._rotulos[plugin_id] = propios

    def get_rotulos(self, plugin_id: str) -> dict[str, str]:
        """Rótulos de los datos de los procedimientos de un plugin ({} si no publicó)."""
        return dict(self._rotulos.get(plugin_id, {}))

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
        self._afirmaciones.pop(plugin_id, None)
        self._referencias.pop(plugin_id, None)
        self._procedimientos.pop(plugin_id, None)
        self._resolvers = {k: v for k, v in self._resolvers.items() if v.plugin_id != plugin_id}
        self._resumenes = {k: v for k, v in self._resumenes.items() if v[1] != plugin_id}
        self._rotulos.pop(plugin_id, None)
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
    write_namespaces: frozenset[str] | None = None,
    agent_id: uuid.UUID | None = None,
    agent_version_id: uuid.UUID | None = None,
    knowledge: Mapping[str, Any] | None = None,
) -> ToolContext:
    return ToolContext(
        conversation_id=conversation_id,
        customer_id=customer_id,
        idempotency_key=idempotency_key,
        channel=channel,
        http=http,
        write_namespaces=write_namespaces,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        knowledge=knowledge,
    )
