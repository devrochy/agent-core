"""Tool `consultar_datos_cliente` (HU-CRM-13, ADR-0179 decisión 3).

La política de datos llega al agente por una tool, no por el prompt: el prefijo cacheado
(HU-AOS-35) no cambia cuando el dueño ajusta un nivel. La tool solo lee: devuelve, por acción,
qué falta, qué se recomienda y QUÉ CLAVES ya están registradas (nunca los valores).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from core.datos_cliente.ajustes import herramientas_activas
from core.datos_cliente.evaluar import evaluar
from core.datos_cliente.modelo import AccionDatos
from core.datos_cliente.registro import registro_datos
from core.datos_cliente.servicio import ajustes_de, base_de
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.memory import entity as entity_memory
from core.shared.logger import get_logger

log = get_logger(__name__)

MAX_ACCIONES = 12


def _agente():
    """El especialista en foco de la release del turno; None fuera de una release."""
    try:
        from core.shared.release_vigente import agente_del_turno

        return agente_del_turno()
    except Exception:
        return None


async def _packs_del_agente() -> set[str]:
    agente = _agente()
    if agente is not None and agente.packs is not None:
        return set(agente.packs)
    try:
        from core.agents.instalaciones_agente import packs_del_negocio

        return await packs_del_negocio(con_core=False)
    except Exception:
        return set()


def _tools_del_agente(agente) -> set[str]:
    """Tools con binding del agente más las que conceden sus superpoderes en las etapas."""
    tools = set(agente.tools)
    try:
        from core.shared.release_vigente import procedimientos_del_agente

        for proc in (procedimientos_del_agente() or {}).values():
            for stage in proc.stages:
                tools.update(stage.actions)
    except Exception:
        pass
    return tools


async def _habilitadas() -> list[AccionDatos]:
    acciones = registro_datos.de_packs(await _packs_del_agente())
    agente = _agente()
    if agente is not None:
        acciones = [a for a in acciones if a.aplica_a(_tools_del_agente(agente))]
    return acciones


async def _hay_acciones(_connection_types: set[str]) -> bool:
    """La tool se ofrece solo si el agente activó sus herramientas de datos (apagado por defecto:
    ofrecer una tool nueva cambia lo que ve el modelo en todos los turnos) y tiene acciones."""
    try:
        agente = _agente()
        if not await herramientas_activas(agente.agent_id if agente is not None else None):
            return False
    except Exception:
        return False
    return bool(await _habilitadas())


class DatosClienteSkill(BaseSkill):
    name = "datos_cliente"
    description = (
        "Consulta qué datos del cliente hacen falta para una acción del negocio (pedido, cita, "
        "inscripción) según la política del negocio, sin devolver los valores ya guardados."
    )
    keywords: ClassVar[list[str]] = []  # siempre activa; el predicado filtra por negocio

    def get_tools(self) -> list[MCPTool]:
        return [replace(t, always_available=True) for t in self._build_tools()]

    def _build_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="consultar_datos_cliente",
                description=(
                    "Dice qué datos del cliente son requeridos, recomendados o no hay que pedir "
                    "para una acción (ej. ventas.pedido_domicilio, agenda.agendar_cita) y cuáles "
                    "ya están registrados. Úsala ANTES de pedirle datos al cliente. Sin `accion` "
                    "lista las acciones del negocio. Un dato recomendado nunca bloquea la acción."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "accion": {
                            "type": "string",
                            "description": "Id de la acción, p. ej. ventas.pedido_domicilio. Opcional.",
                        }
                    },
                    "required": [],
                },
                handler=self.consultar,
                connection_predicate=_hay_acciones,
                solo_lectura=True,
            )
        ]

    async def consultar(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        acciones = await _habilitadas()
        pedida = str(args.get("accion") or "").strip()
        if pedida:
            acciones = [a for a in acciones if a.id == pedida]
            if not acciones:
                return {
                    "error": "Esa acción no existe en este negocio.",
                    "acciones": [a.id for a in await _habilitadas()],
                }
        try:
            perfil = await entity_memory.get_profile(ctx.customer_id)
        except Exception:
            log.warning("datos_cliente_perfil_no_disponible", exc_info=True)
            perfil = {}
        salida: list[dict[str, Any]] = []
        for accion in acciones[:MAX_ACCIONES]:
            ev = evaluar(
                accion, perfil, await ajustes_de(accion.id, ctx.agent_id), await base_de(accion)
            )
            salida.append({"etiqueta": accion.etiqueta, **ev.como_sobre()})
        return {
            "status": "ok",
            "acciones": salida,
            "hint": (
                "Pide solo lo de `faltan`, de a uno y con naturalidad. Lo de `recomendados_pendientes` "
                "se pide si encaja, pero la acción sigue sin ello. No pidas lo que ya está en "
                "`ya_registrados`."
            ),
        }


__all__ = ["DatosClienteSkill"]
