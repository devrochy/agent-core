"""Simuladores de Captar leads para la prueba del panel (HU-ARCH-14, ADR-0193).

No escriben en las tablas de leads: el lead de la conversación `test` vive en el `SandboxPrueba` (Redis). Vivían en
`pack_commerce.sandbox`; conservan la colección `leads` del sandbox.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.mcp.sandbox import SandboxPrueba
from core.mcp.types import ToolContext


async def _sandbox(context: ToolContext) -> SandboxPrueba:
    if context.sandbox is None:
        raise RuntimeError("El simulador de leads requiere sandbox de prueba")
    return context.sandbox


async def simular_crear_lead(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    existente = await sandbox.buscar("leads", "customer_id", str(context.customer_id))
    if existente is not None:
        for campo in ("name", "email", "phone", "interest"):
            if arguments.get(campo):
                existente[campo] = str(arguments[campo])
        await sandbox.guardar("leads", existente["lead_id"], existente)
        return {"lead_id": existente["lead_id"], "status": "actualizado"}
    lead_id = str(uuid.uuid4())
    await sandbox.guardar(
        "leads",
        lead_id,
        {
            "lead_id": lead_id,
            "customer_id": str(context.customer_id),
            "conversation_id": str(context.conversation_id),
            "name": arguments.get("name"),
            "email": arguments.get("email"),
            "phone": arguments.get("phone"),
            "interest": arguments.get("interest", ""),
            "status": "nuevo",
        },
    )
    return {"lead_id": lead_id, "status": "created"}


async def simular_calificar_lead(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    try:
        score = max(1, min(10, int(arguments.get("score", 0))))
    except (TypeError, ValueError):
        return {"error": "score inválido: debe ser un número entre 1 y 10."}
    sandbox = await _sandbox(context)
    lead = await sandbox.buscar("leads", "customer_id", str(context.customer_id))
    if lead is None:
        return {"error": "No hay lead para esta conversación. Usa crear_lead primero."}
    lead["status"] = "calificado"
    lead["score"] = score
    await sandbox.guardar("leads", lead["lead_id"], lead)
    return {"lead_id": lead["lead_id"], "score": score, "status": "calificado"}


async def simular_actualizar_lead(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    lead = await sandbox.buscar("leads", "customer_id", str(context.customer_id))
    if lead is None:
        return {"error": "No hay lead para esta conversación. Usa crear_lead primero."}
    for campo in ("name", "email", "phone", "interest", "negocio", "status"):
        if arguments.get(campo):
            lead[campo] = str(arguments[campo])
    await sandbox.guardar("leads", lead["lead_id"], lead)
    return {"lead_id": lead["lead_id"], "status": "actualizado"}
