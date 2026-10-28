"""Tool `responder_aviso_datos` (HU-CRM-16). Solo se ofrece si el negocio activó el aviso de datos."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from core.datos_cliente.aviso_chat import aviso_activo, responder
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared.tenant import get_current_tenant_id


async def _aviso_encendido(_connection_types: set[str]) -> bool:
    try:
        return await aviso_activo(get_current_tenant_id())
    except Exception:
        return False


class AvisoDatosSkill(BaseSkill):
    name = "aviso_datos"
    description = "Registra la respuesta del cliente al aviso de tratamiento de datos."
    keywords: ClassVar[list[str]] = []

    def get_tools(self) -> list[MCPTool]:
        return [replace(t, always_available=True) for t in self._build_tools()]

    def _build_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="responder_aviso_datos",
                description=(
                    "Úsala SOLO cuando el cliente responda al aviso de datos que el sistema le mostró: "
                    "decision='acepto' si dijo que acepta, 'rechazo' si dijo que no. El sistema verifica "
                    "su último mensaje; si no es claro, no registra nada. No redactes ni repitas el aviso."
                ),
                parameters={
                    "type": "object",
                    "properties": {"decision": {"type": "string", "enum": ["acepto", "rechazo"]}},
                    "required": ["decision"],
                },
                handler=self.responder,
                connection_predicate=_aviso_encendido,
            )
        ]

    async def responder(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return {"error": "Sin negocio en contexto."}
        return await responder(tenant_id, ctx.conversation_id, ctx.customer_id, str(args.get("decision") or ""))
