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
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

MAX_ACCIONES = 12


async def _habilitadas() -> list[AccionDatos]:
    from core.agents.spec import enabled_packs, load_active_spec

    try:
        return registro_datos.de_packs(enabled_packs(await load_active_spec()))
    except Exception:
        return []


async def _hay_acciones(_connection_types: set[str]) -> bool:
    """La tool se ofrece solo si el negocio activó las herramientas de datos del cliente (apagado por
    defecto: ofrecer una tool nueva cambia lo que ve el modelo en todos los turnos) y hay acciones."""
    try:
        if not await herramientas_activas(get_current_tenant_id()):
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
            ev = evaluar(accion, perfil, await ajustes_de(accion.id), await base_de(accion))
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
