"""Simuladores de Cobranza para la prueba del panel (HU-ARCH-14).

Promesas, abonos, suscripciones y links de pago quedan en el sandbox; no se escribe
en la cartera real ni se llama a una pasarela. Los saldos y la cartera que se consultan
son reales (solo lectura).
"""

from __future__ import annotations

import uuid
from typing import Any

from core.mcp.sandbox import SandboxPrueba
from core.mcp.types import ToolContext


async def _sandbox(context: ToolContext) -> SandboxPrueba:
    if context.sandbox is None:
        raise RuntimeError("El simulador de cobranza requiere sandbox de prueba")
    return context.sandbox


async def simular_registrar_promesa(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    promesa_id = str(uuid.uuid4())
    await sandbox.guardar(
        "promises",
        promesa_id,
        {
            "promesa_id": promesa_id,
            "customer_id": str(context.customer_id),
            "fecha": str(arguments.get("fecha") or ""),
            "valor": float(arguments.get("valor") or 0),
            "tipo": str(arguments.get("tipo") or "promesa"),
        },
    )
    return {
        "registrada": True,
        "promesa_id": promesa_id,
        "fecha": str(arguments.get("fecha") or ""),
        "valor": float(arguments.get("valor") or 0),
    }


async def simular_registrar_promesa_saldo(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    resultado = await simular_registrar_promesa(arguments, context)
    resultado["alcance"] = str(arguments.get("alcance") or "vencido")
    return resultado


async def simular_registrar_abono(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    abono_id = str(uuid.uuid4())
    valor = float(arguments.get("valor") or 0)
    await sandbox.guardar(
        "payments",
        abono_id,
        {
            "abono_id": abono_id,
            "customer_id": str(context.customer_id),
            "valor": valor,
            "metodo_pago": str(arguments.get("metodo_pago") or "efectivo"),
        },
    )
    return {"registrado": True, "abono_id": abono_id, "valor": valor}


async def _sub(context: ToolContext) -> dict[str, Any] | None:
    return await (await _sandbox(context)).buscar("subscriptions", "customer_id", str(context.customer_id))


async def simular_consultar_suscripcion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sub = await _sub(context)
    if sub is None:
        return {"encontrado": False, "mensaje": "No tenés una suscripción registrada en la prueba."}
    return {"encontrado": True, **sub}


async def simular_crear_suscripcion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    existente = await _sub(context)
    if existente is not None:
        return {
            "ya_existe": True,
            "suscripcion_id": existente["suscripcion_id"],
            "estado": existente.get("estado", "activa"),
        }
    sub_id = str(uuid.uuid4())
    sub = {
        "suscripcion_id": sub_id,
        "customer_id": str(context.customer_id),
        "concepto": str(arguments.get("concepto") or "Servicio"),
        "valorPeriodo": float(arguments.get("valor") or 0),
        "estado": "activa",
        "periodicidad": "mensual",
    }
    await sandbox.guardar("subscriptions", sub_id, sub)
    return {"creada": True, **sub}


async def _cambiar_estado(arguments: dict[str, Any], context: ToolContext, estado: str) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    sub = await _sub(context)
    if sub is None:
        return {"encontrado": False, "mensaje": "No tenés una suscripción registrada en la prueba."}
    sub["estado"] = estado
    await sandbox.guardar("subscriptions", sub["suscripcion_id"], sub)
    return {"estado": estado, "suscripcion_id": sub["suscripcion_id"]}


async def simular_pausar_suscripcion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _cambiar_estado(arguments, context, "pausada")


async def simular_cancelar_suscripcion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _cambiar_estado(arguments, context, "cancelada")


async def simular_renovar_suscripcion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return await _cambiar_estado(arguments, context, "activa")


async def simular_link_suscripcion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sub = await _sub(context)
    if sub is None:
        return {"encontrado": False, "mensaje": "No tenés una suscripción registrada en la prueba."}
    link = f"https://prueba.local/cobranza/{sub['suscripcion_id']}"
    return {
        "encontrado": True,
        "link": link,
        "url": link,
        "valor": sub.get("valorPeriodo", 0),
        "_safe_append": f"💳 Podés pagar tu cuota aquí: {link} (simulado)",
    }


__all__ = [
    "simular_cancelar_suscripcion",
    "simular_consultar_suscripcion",
    "simular_crear_suscripcion",
    "simular_link_suscripcion",
    "simular_pausar_suscripcion",
    "simular_registrar_abono",
    "simular_registrar_promesa",
    "simular_registrar_promesa_saldo",
    "simular_renovar_suscripcion",
]
