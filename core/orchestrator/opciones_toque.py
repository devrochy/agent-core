"""Cableado del toque de una opción directa con el orquestador (SPEC-40, ADR-0172 §4, HU-OPC-03).

El flujo y sus reglas viven en `core/kernel/opciones/resolver.py`; el kernel no importa el orquestador, así
que lo que solo el loop sabe se inyecta desde aquí (`Bordes`): el entorno de una tool FUERA de un turno y el
envío de mensajes por la cola normal.

**Entorno fuera del loop** (el punto frágil del ADR): un turno resuelve el spec y la release con
`load_active_spec` → `fijar_release_del_turno` → `spec_del_turno`, bajo `con_release`. El toque lo repite igual
para que la tool corra con los MISMOS bindings que tendría en un turno del agente en foco (con
`aos_runtime = v3`, los bindings son los de la versión congelada de ese agente; con legacy, los del
AgentSpec). Es la misma asignación sticky por episodio que haría el siguiente mensaje del cliente: abre el
episodio y puede fijar la release y el dueño de la conversación, pero no cambia nada que el próximo turno no
fuera a cambiar igual. Sin turno abierto, `anotar_release` y `anotar_ruta` no hacen nada. La ranura se abre y
se cierra aquí (también si falla): lo que fija un toque no lo ve el siguiente turno.

La ficha de la tool (`FichaTool`) sale de las mismas piezas que el lint (`lint.fichas_de_tools`), con el
binding EXPLÍCITO del spec del turno y los procedimientos disponibles de la release: la regla de elegibilidad
es `lint.motivos_de_rechazo`, la misma que aplicó el lint al guardar.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from core.agents.spec import enabled_packs, load_active_spec
from core.agents.spec_de_release import spec_del_turno
from core.kernel.opciones import resolver
from core.kernel.opciones.lint import FichaTool, acciones_de_procedimientos, fichas_de_tools
from core.kernel.opciones.textos import texto
from core.kernel.release_del_turno import fijar_release_del_turno
from core.llm.types import LLMToolCall
from core.shared import release_vigente
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

PREFIJO_DECLARATIVAS = "declarative"


class _EntornoDelTurno:
    """`Entorno` del resolver: spec, release y tools del negocio, resueltos como en un turno."""

    def __init__(self, conversation: Any, customer: Any, spec: Any) -> None:
        self._conversation = conversation
        self._customer = customer
        self._spec = spec

    async def ficha(self, tool: str) -> FichaTool | None:
        from core.kernel.procedures import registro
        from core.mcp.declarative import tools_for_spec
        from core.mcp.host import mcp_host
        from core.plugin_manager import plugin_manager

        spec = self._spec
        registrada = mcp_host.get_tool(tool)
        # Prioridad al host, como el loop: una declarativa no puede ocultar una tool registrada.
        declarativa = None if registrada is not None else next((t for t in tools_for_spec(spec) if t.name == tool), None)
        elegida = registrada or declarativa
        if elegida is None:
            return None
        habilitados = enabled_packs(spec)
        inactivos = await plugin_manager.disabled_plugins_for_tenant(get_current_tenant_id())
        packs_activos = set(habilitados) - set(inactivos)
        if declarativa is not None:
            packs_activos.add(PREFIJO_DECLARATIVAS)  # las declarativas del negocio valen por estar en su spec
        gobernadas = acciones_de_procedimientos(registro.disponibles(habilitados, inactivos).values())
        return fichas_de_tools([elegida], spec.tools, packs_activos, gobernadas).get(tool)

    async def denegacion(self, tool: str) -> str | None:
        from core.orchestrator.loop import _tool_denial

        return await _tool_denial(self._spec, tool, self._conversation.id)

    async def ejecutar(self, tool: str, args: dict[str, Any], activation_id: uuid.UUID) -> dict[str, Any]:
        from core.orchestrator.loop import _execute_tool_safely

        # `id` estable = `activation_id`: la `idempotency_key` de la tool es `<conversación>:<activación>`
        # (mismo precedente que `_ejecutor_del_motor`). El cliente ya confirmó si la acción lo pedía.
        llamada = LLMToolCall(id=str(activation_id), name=tool, arguments=args)
        return await _execute_tool_safely(
            self._conversation, self._customer, llamada, self._spec, ya_confirmada=True
        )


def abrir_entorno(conversation: Any, customer: Any) -> AbstractAsyncContextManager[_EntornoDelTurno]:
    @asynccontextmanager
    async def _abrir() -> AsyncIterator[_EntornoDelTurno]:
        token = release_vigente.abrir()
        try:
            spec = await load_active_spec()
            await fijar_release_del_turno(conversation, texto="")
            yield _EntornoDelTurno(conversation, customer, spec_del_turno(spec))
        finally:
            release_vigente.cerrar(token)

    return _abrir()


async def atender(conversation: Any, customer: Any, fields: dict[str, str]) -> str:
    """Atiende el toque de la conversación. Devuelve el `outcome` (ver `resolver`)."""
    from core.gateway.sender import (
        interactivo_valido,
        send_interactive_to_conversation,
        send_to_conversation,
    )

    conversation_id = conversation.id

    async def enviar_texto(cuerpo: str) -> bool:
        return await send_to_conversation(conversation_id, cuerpo)

    async def enviar_confirmacion(interactivo: dict[str, Any]) -> bool:
        # Un botón solo existe en WhatsApp y dentro de los topes de Meta: si no se puede mostrar, la
        # confirmación no se pide (un texto no confirma nada) y el resolver avisa que no se hizo nada.
        if conversation.channel != "whatsapp" or not interactivo_valido(interactivo):
            return False
        return await send_interactive_to_conversation(
            conversation_id, interactivo, fallback_text=texto("confirmacion_no_disponible")
        )

    toque = resolver.Toque(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation_id,
        payload=(fields.get("interactive_id") or "").strip()[:256],
        wamid=fields.get("external_message_id") or None,
    )
    bordes = resolver.Bordes(
        abrir_entorno=lambda: abrir_entorno(conversation, customer),
        enviar_texto=enviar_texto,
        enviar_confirmacion=enviar_confirmacion,
    )
    return await resolver.atender_toque(toque, bordes)
