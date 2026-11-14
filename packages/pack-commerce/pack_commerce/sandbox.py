"""Simuladores de Comercio para la prueba del panel (HU-ARCH-14).

Ninguna función de este módulo escribe en las tablas del pack: cotizaciones, pedidos,
leads y links de pago viven en el `SandboxPrueba` (Redis) de la conversación `test`.
Las lecturas de catálogo/inventario sí son reales (solo lectura).

El número de pedido simulado usa `PRUEBA-0001`, un contador propio del sandbox: nunca
toca `crm_order_counters` ni consume el consecutivo real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from core.mcp.sandbox import SandboxPrueba, numeracion
from core.mcp.types import ToolContext


async def _sandbox(context: ToolContext) -> SandboxPrueba:
    if context.sandbox is None:
        raise RuntimeError("El simulador de comercio requiere sandbox de prueba")
    return context.sandbox


# ---------------------------------------------------------------- leads
async def simular_crear_lead(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    existente = await sandbox.buscar("leads", "customer_id", str(context.customer_id))
    if existente is not None:
        return {
            "lead_id": existente["lead_id"],
            "status": "ya_existe",
            "hint": "Este cliente ya es un lead. Usa actualizar_lead.",
        }
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
    for campo in ("name", "email", "phone", "interest", "status"):
        if arguments.get(campo):
            lead[campo] = str(arguments[campo])
    await sandbox.guardar("leads", lead["lead_id"], lead)
    return {"lead_id": lead["lead_id"], "status": "actualizado"}


# ---------------------------------------------------------------- cotización
async def _calcular_cotizacion(arguments: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | dict[str, Any]:
    from .skills import _get_variant_with_stock

    items = arguments.get("items") or []
    if not items or not isinstance(items, list):
        return {"error": "Necesito al menos un producto para cotizar."}
    rows: list[dict[str, Any]] = []
    subtotal = 0.0
    for item in items:
        if not isinstance(item, dict):
            return {"error": "Cada producto de la cotización debe traer variant_id y quantity."}
        variant_ref = str(item.get("variant_id", ""))
        try:
            quantity = int(item.get("quantity", 0))
        except (TypeError, ValueError):
            return {"error": "Cantidad inválida para un producto de la cotización."}
        if quantity <= 0:
            return {"error": "Las cantidades deben ser números enteros positivos."}
        data = await _get_variant_with_stock(variant_ref)
        if data is None or not data["variant"].active:
            return {"error": "Uno de los productos solicitados no está disponible."}
        if not data["sin_inventario"] and data["stock"] < quantity:
            return {
                "error": f"No hay stock suficiente de {data['product'].name} "
                f"(disponible: {data['stock']})."
            }
        line_total = data["price"] * quantity
        subtotal += line_total
        rows.append(
            {
                "variant_id": str(data["variant"].id),
                "product_name": data["product"].name,
                "attributes": data["variant"].attributes or {},
                "unit_price": data["price"],
                "quantity": quantity,
                "line_total": line_total,
            }
        )
    return ({"items": rows, "subtotal": round(subtotal, 2)}, rows)


async def simular_generar_cotizacion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    from .skills import _plugin_config

    calculo = await _calcular_cotizacion(arguments)
    if isinstance(calculo, dict) and "error" in calculo:
        return calculo
    datos, rows = calculo  # type: ignore[misc]
    sandbox = await _sandbox(context)
    quote_id = str(uuid.uuid4())
    total = datos["subtotal"]
    quote = {
        "quote_id": quote_id,
        "customer_id": str(context.customer_id),
        "conversation_id": str(context.conversation_id),
        "status": "draft",
        "items": rows,
        "subtotal": total,
        "discount": 0.0,
        "total": total,
        "applied_promotion_id": None,
        "creado": datetime.now(UTC).isoformat(),
    }
    await sandbox.guardar("quotes", quote_id, quote)
    config = await _plugin_config()
    return {
        "quote_id": quote_id,
        "status": "draft",
        "items": [
            {
                "product": r["product_name"],
                "attributes": r["attributes"],
                "quantity": r["quantity"],
                "unit_price": r["unit_price"],
                "line_total": r["line_total"],
            }
            for r in rows
        ],
        "subtotal": total,
        "total": total,
        "moneda": getattr(config, "currency_code", "COP") or "COP",
    }


async def simular_aplicar_promocion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    quote = await sandbox.leer("quotes", str(arguments.get("quote_id", "")))
    if quote is None:
        return {"error": "Cotización simulada no encontrada."}
    return {
        "quote_id": quote["quote_id"],
        "applied": False,
        "subtotal": quote["subtotal"],
        "total": quote["total"],
        "segmento": "curioso",
        "motivo": "no_aplica",
    }


async def simular_confirmar_cotizacion(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    quote = await sandbox.leer("quotes", str(arguments.get("quote_id", "")))
    if quote is None:
        return {"error": "Cotización simulada no encontrada."}
    if quote["status"] == "convertida":
        return {"error": "Esta cotización ya se convirtió en un pedido."}
    quote["status"] = "aceptada"
    await sandbox.guardar("quotes", quote["quote_id"], quote)
    return {"quote_id": quote["quote_id"], "status": "aceptada", "total": quote["total"]}


# ---------------------------------------------------------------- pedido
async def simular_crear_pedido(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    quote = await sandbox.leer("quotes", str(arguments.get("quote_id", "")))
    if quote is None:
        return {"error": "Cotización simulada no encontrada."}
    if quote["status"] != "aceptada":
        return {
            "error": "La cotización todavía no fue confirmada por el cliente.",
            "hint": "Llamá confirmar_cotizacion primero.",
        }
    existing = await sandbox.buscar("orders", "quote_id", quote["quote_id"])
    if existing is not None:
        return {
            "order_id": existing["order_id"],
            "status": "duplicate",
            "public_order_number": existing["public_order_number"],
        }
    order_id = str(uuid.uuid4())
    seq = await sandbox.siguiente("order")
    public_number = numeracion("PRUEBA", seq)
    customer_data = arguments.get("customer_data") or {}
    order = {
        "order_id": order_id,
        "quote_id": quote["quote_id"],
        "customer_id": str(context.customer_id),
        "conversation_id": str(context.conversation_id),
        "public_order_number": public_number,
        "status": "abierto",
        "payment_status": "pendiente",
        "payment_method": arguments.get("payment_method"),
        "delivery_method": arguments.get("delivery_method"),
        "total": quote["total"],
        "items": quote["items"],
        "creado": datetime.now(UTC).isoformat(),
    }
    await sandbox.guardar("orders", order_id, order)
    quote["status"] = "convertida"
    await sandbox.guardar("quotes", quote["quote_id"], quote)
    entrega = ", ".join(
        str(customer_data.get(k) or "") for k in ("address", "municipality", "city")
    ).strip(", ")
    return {
        "order_id": order_id,
        "status": "confirmed",
        "public_order_number": public_number,
        "total": quote["total"],
        "payment_method": arguments.get("payment_method"),
        "delivery_method": arguments.get("delivery_method"),
        "entregado_en": entrega,
        "_safe_append": f"Tu pedido {public_number} quedó confirmado (simulado).",
    }


async def simular_agregar_item_pedido(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    from .skills import _get_variant_with_stock

    sandbox = await _sandbox(context)
    order = await sandbox.leer("orders", str(arguments.get("order_id", "")))
    if order is None:
        return {"error": "Pedido simulado no encontrado."}
    additions: list[tuple[str, int, float]] = []
    add_total = 0.0
    for item in arguments.get("items") or []:
        try:
            variant_ref = str(item.get("variant_id", ""))
            quantity = int(item.get("quantity", 0))
        except (TypeError, ValueError, AttributeError):
            return {"error": "Cantidad inválida."}
        if quantity <= 0:
            return {"error": "Las cantidades deben ser positivas."}
        data = await _get_variant_with_stock(variant_ref)
        if data is None or not data["variant"].active:
            return {"error": "Uno de los productos no está disponible."}
        if not data["sin_inventario"] and data["stock"] < quantity:
            return {"error": f"No hay stock suficiente de {data['product'].name}."}
        additions.append((data["variant"].sku, quantity, data["price"]))
        add_total += data["price"] * quantity
    order["total"] = round(float(order["total"]) + add_total, 2)
    await sandbox.guardar("orders", order["order_id"], order)
    return {
        "order_id": order["order_id"],
        "status": "updated",
        "nuevo_total": order["total"],
        "agregados": [{"sku": sku, "quantity": q} for sku, q, _p in additions],
    }


async def simular_confirmar_pago_pedido(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    order = await sandbox.leer("orders", str(arguments.get("order_id", "")))
    if order is None:
        return {"error": "Pedido simulado no encontrado."}
    if order.get("payment_status") == "pagado":
        return {
            "order_id": order["order_id"],
            "status": "pagado",
            "public_order_number": order["public_order_number"],
            "message": "Este pedido ya está pagado.",
        }
    numero = order["public_order_number"]
    link = f"https://prueba.local/pago/{numero}"
    order["payment_reference"] = numero
    order["payment_link_url"] = link
    await sandbox.guardar("orders", order["order_id"], order)
    return {
        "order_id": order["order_id"],
        "status": "link_creado",
        "public_order_number": numero,
        "monto": order["total"],
        "link": link,
        "_safe_append": f"💳 Podés pagar tu pedido {numero} aquí: {link} (simulado)",
    }


async def simular_verificar_pago_pedido(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    order = await sandbox.leer("orders", str(arguments.get("order_id", "")))
    if order is None:
        return {"error": "pedido_no_encontrado"}
    if order.get("payment_status") == "pagado":
        return {
            "order_id": order["order_id"],
            "public_order_number": order["public_order_number"],
            "pago_confirmado": "confirmado",
        }
    return {
        "error": "pago_pendiente",
        "detail": f"El pago del pedido {order['public_order_number']} todavía no se ha confirmado.",
        "hint": "Si ya pagó, puede tardar unos minutos. Si el link venció, confirmar_pago_pedido genera uno nuevo.",
    }


async def simular_consultar_estado_pedido(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    sandbox = await _sandbox(context)
    numero = str(arguments.get("public_order_number", "")).strip().upper()
    order = await sandbox.buscar("orders", "public_order_number", numero)
    if order is None:
        return {"found": False, "hint": "No encontré un pedido simulado con ese número para este cliente."}
    return {
        "found": True,
        "public_order_number": order["public_order_number"],
        "status": order["status"],
        "delivery_method": "domicilio" if order.get("delivery_method") == "domicilio" else "recoger en tienda",
        "payment_status": "pagado" if order.get("payment_status") == "pagado" else "pendiente de pago",
        "items": [
            {
                "product": item.get("product_name", "Producto"),
                "quantity": item.get("quantity", 0),
                "unit_price": item.get("unit_price", 0),
            }
            for item in order.get("items", [])
        ],
        "total": order.get("total", 0),
    }


__all__ = [
    "simular_actualizar_lead",
    "simular_agregar_item_pedido",
    "simular_aplicar_promocion",
    "simular_calificar_lead",
    "simular_confirmar_cotizacion",
    "simular_confirmar_pago_pedido",
    "simular_consultar_estado_pedido",
    "simular_crear_lead",
    "simular_crear_pedido",
    "simular_generar_cotizacion",
    "simular_verificar_pago_pedido",
]
