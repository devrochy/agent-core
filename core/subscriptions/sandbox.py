"""Simuladores de suscripciones para la prueba del panel (HU-ARCH-14).

No se llama al proveedor ni se escribe en sus tablas: la suscripción, los cargos y los
links de pago viven en el sandbox de la conversación `test`.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.mcp.sandbox import SandboxPrueba
from core.mcp.types import ToolContext

_SIN = {
    "encontrado": False,
    "mensaje": "No tenés una suscripción activa en la prueba.",
}


async def _sandbox(context: ToolContext) -> SandboxPrueba:
    if context.sandbox is None:
        raise RuntimeError("El simulador de suscripciones requiere sandbox de prueba")
    return context.sandbox


async def _suscripcion(context: ToolContext) -> dict[str, Any] | None:
    sandbox = await _sandbox(context)
    return await sandbox.buscar("subscriptions", "customer_id", str(context.customer_id))


async def simular_consultar(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    suscripcion = await _suscripcion(context)
    if suscripcion is None:
        return dict(_SIN)
    return {"encontrado": True, **suscripcion}


async def simular_link_pago(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    suscripcion = await _suscripcion(context)
    numero = (suscripcion or {}).get("id", str(uuid.uuid4()))
    link = f"https://prueba.local/suscripcion/{numero}"
    return {
        "status": "link_creado",
        "link": link,
        "url": link,
        "monto": (suscripcion or {}).get("valor_periodo", 0),
        "_safe_append": f"💳 Podés pagar tu suscripción aquí: {link} (simulado)",
    }


async def simular_pausar(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    suscripcion = await _suscripcion(context)
    if suscripcion is None:
        return dict(_SIN)
    suscripcion["estado"] = "pausada"
    await (await _sandbox(context)).guardar("subscriptions", suscripcion["id"], suscripcion)
    return {"status": "pausada", "estado": "pausada", "encontrado": True, **suscripcion}


async def simular_cancelar(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    suscripcion = await _suscripcion(context)
    if suscripcion is None:
        return dict(_SIN)
    suscripcion["estado"] = "cancelada"
    await (await _sandbox(context)).guardar("subscriptions", suscripcion["id"], suscripcion)
    return {"status": "cancelada", "estado": "cancelada", "encontrado": True, **suscripcion}


async def _cambiar(arguments: dict[str, Any], context: ToolContext, *, confirmar: bool = False) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    suscripcion = await sandbox.buscar("subscriptions", "customer_id", str(context.customer_id))
    if suscripcion is None:
        suscripcion = {
            "id": str(uuid.uuid4()),
            "customer_id": str(context.customer_id),
            "plan_code": str(arguments.get("plan_code") or "PLAN-PRUEBA"),
            "valor_periodo": 0,
            "estado": "activa",
        }
    if confirmar:
        suscripcion["plan_code"] = str(arguments.get("plan_code") or suscripcion["plan_code"])
    await sandbox.guardar("subscriptions", suscripcion["id"], suscripcion)
    return {
        "encontrado": True,
        "status": "cambio_simulado",
        "plan_code": suscripcion["plan_code"],
        "suscripcion": suscripcion,
    }


async def simular_cambiar_plan(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _cambiar(arguments, context, confirmar=True)


async def simular_aplicar_cambio_plan(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _cambiar(arguments, context, confirmar=True)


async def simular_confirmar_inscripcion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _cambiar(arguments, context, confirmar=False)


async def simular_inscribir(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    suscripcion = {
        "id": str(uuid.uuid4()),
        "customer_id": str(context.customer_id),
        "plan_code": str(arguments.get("plan_code") or "PLAN-PRUEBA"),
        "valor_periodo": 0,
        "estado": "activa",
    }
    await sandbox.guardar("subscriptions", suscripcion["id"], suscripcion)
    link = f"https://prueba.local/suscripcion/{suscripcion['id']}"
    return {
        "status": "inscrita",
        "encontrado": True,
        "suscripcion": suscripcion,
        "link": link if arguments.get("payment_method") == "pago_en_linea" else None,
        "_safe_append": f"Tu inscripción quedó registrada (simulada): {suscripcion['plan_code']}.",
    }


__all__ = [
    "simular_aplicar_cambio_plan",
    "simular_cambiar_plan",
    "simular_cancelar",
    "simular_confirmar_inscripcion",
    "simular_consultar",
    "simular_inscribir",
    "simular_link_pago",
    "simular_pausar",
]
