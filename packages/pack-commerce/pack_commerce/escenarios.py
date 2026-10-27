"""Escenarios de datos de prueba de Comercio (HU-QA-13, adenda de ADR-0135 §8).

- `clientes-y-pedidos`: un lead por cliente (estado e interés), pedidos pendiente, pagado y
  cancelado sobre el catálogo de «Preparar mis datos» y una promoción.
- `planes-de-suscripcion`: dos planes nuevos (con el sufijo del caso en el SKU) y su snapshot,
  que el core le pasa a Suscripciones.

El contenido sale de `presets/escenarios.json`. Determinista y aditivo: cada caso usa claves de
idempotencia con su sufijo. Nada se cobra ni se envía: los pedidos no llevan link de pago y el
estado «pagado» se escribe en la columna, sin emitir `order.paid` (que activaría planes).
Los leads no llevan conversación, así que el seguimiento automático (`jobs.py`) no les escribe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from core.db.session import get_session
from core.qa.contrato_datos import (
    ContextoDeNegocio,
    EntradaDeEscenario,
    EscenarioNoDisponible,
    PlanDeEscenario,
    ResumenDeEscenario,
)
from core.shared.tenant import get_current_tenant_id
from pack_commerce.models import Lead, Order, OrderItem, Product, ProductVariant, Promotion


def _contenido() -> dict[str, Any]:
    from core.presets import load_fragment

    return dict(load_fragment("pack_commerce", "escenarios").seed)


async def _variantes(session: Any, tenant_id: uuid.UUID, skus: set[str]) -> dict[str, ProductVariant]:
    filas = (
        await session.execute(
            select(ProductVariant).where(ProductVariant.tenant_id == tenant_id, ProductVariant.sku.in_(skus))
        )
    ).scalars().all()
    encontradas = {v.sku: v for v in filas}
    if skus - set(encontradas):
        raise EscenarioNoDisponible(
            "Falta el catálogo de «Preparar mis datos» (" + ", ".join(sorted(skus - set(encontradas))) + ")."
        )
    return encontradas


async def clientes_y_pedidos(tenant_id: uuid.UUID, entrada: EntradaDeEscenario) -> ResumenDeEscenario:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("escenario fuera del tenant_scope del negocio")
    datos = _contenido()
    clientes = entrada.clientes
    if not clientes:
        raise EscenarioNoDisponible("El escenario necesita clientes.")
    ahora = datetime.now(UTC)
    skus = {linea["sku"] for p in datos["pedidos"] for linea in p["lineas"]} | {datos["promocion"]["sku"]}
    async with get_session() as session:
        variantes = await _variantes(session, tenant_id, skus)
        for n, (cliente, lead) in enumerate(zip(clientes, datos["leads"], strict=False)):
            session.add(
                Lead(
                    tenant_id=tenant_id,
                    customer_id=cliente.customer_id,
                    name=cliente.nombre,
                    interest=lead["interes"],
                    score=[8, 3, 9, 5, 1, 7][n % 6],
                    status=lead["estado"],
                    idempotency_key=f"qa-escenario:{entrada.sufijo}:{cliente.customer_id}",
                )
            )
        numeros: list[str] = []
        for n, pedido in enumerate(datos["pedidos"]):
            cliente = clientes[pedido["cliente"] % len(clientes)]
            seq = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": tenant_id})).scalar_one()
            lineas = [(variantes[ln["sku"]], int(ln["cantidad"])) for ln in pedido["lineas"]]
            total = sum((Decimal(str(v.price)) * c for v, c in lineas), Decimal(0))
            orden = Order(
                tenant_id=tenant_id,
                conversation_id=cliente.conversation_id,
                customer_id=cliente.customer_id,
                status=pedido["estado"],
                payment_status=pedido["pago"],
                payment_method=pedido["metodo"],
                delivery_method=pedido["entrega"],
                total=total,
                idempotency_key=f"qa-escenario:{entrada.sufijo}:{n}",
                order_seq=seq,
                public_order_number=f"OC-{seq:04d}",
                delivery_full_name=cliente.nombre,
                delivery_city="Manizales",
                status_changed_at=ahora if pedido["estado"] != "abierto" else None,
                status_reason=pedido.get("motivo"),
            )
            session.add(orden)
            await session.flush()
            for variante, cantidad in lineas:
                session.add(
                    OrderItem(
                        tenant_id=tenant_id, order_id=orden.id, variant_id=variante.id, quantity=cantidad,
                        unit_price=variante.price,
                    )
                )
            numeros.append(f"{orden.public_order_number} ({pedido['estado']}, {pedido['pago']})")
        promo = datos["promocion"]
        variante = variantes[promo["sku"]]
        session.add(
            Promotion(
                tenant_id=tenant_id,
                type=promo["tipo"],
                rules={"discount_pct": promo["descuento_pct"]},
                valid_from=ahora,
                valid_to=ahora + timedelta(days=int(promo["dias"])),
                active=True,
                product_id=variante.product_id,
            )
        )
    return ResumenDeEscenario(
        conteos={"leads": min(len(clientes), len(datos["leads"])), "pedidos": len(numeros), "promociones": 1},
        detalle=[
            "Un lead por cliente con su estado e interés",
            "Pedidos " + ", ".join(numeros),
            f"Promoción de temporada: {promo['descuento_pct']} % en {promo['sku']} por {promo['dias']} días",
        ],
    )


async def planes_de_suscripcion(tenant_id: uuid.UUID, entrada: EntradaDeEscenario) -> ResumenDeEscenario:
    from pack_commerce.datos_de_prueba import PlanesDePrueba, sembrar
    from pack_commerce.planes import snapshot_de_plan

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("escenario fuera del tenant_scope del negocio")
    planes = PlanesDePrueba.model_validate(_contenido()["planes"])
    contexto = ContextoDeNegocio(moneda=entrada.moneda, zona_horaria=entrada.zona_horaria, sufijo=entrada.sufijo)
    registros = await sembrar(tenant_id, "planes", planes, contexto)
    ids = [r.ref for r in registros if r.kind == "variante"]
    async with get_session() as session:
        filas = (
            await session.execute(
                select(ProductVariant, Product.name)
                .join(Product, (Product.tenant_id == ProductVariant.tenant_id) & (Product.id == ProductVariant.product_id))
                .where(ProductVariant.tenant_id == tenant_id, ProductVariant.id.in_(ids))
                .order_by(ProductVariant.sku)
            )
        ).all()
    salida: list[PlanDeEscenario] = []
    for variante, nombre in filas:
        plan, _ignorada = snapshot_de_plan(
            sku=variante.sku, nombre=nombre, precio=variante.price, periodicidad=variante.billing_period or "mensual",
            trial_days=variante.trial_days,
        )
        salida.append(PlanDeEscenario(variant_id=variante.id, plan=plan))
    if len(salida) < 2:
        raise EscenarioNoDisponible("No se pudieron crear los planes de prueba.")
    return ResumenDeEscenario(
        conteos={"planes": len(salida)},
        detalle=["Planes " + " y ".join(f"{p.plan['nombre']} ({p.plan['periodicidad']})" for p in salida)],
        planes=salida,
    )


async def sembrar_escenario(tenant_id: uuid.UUID, escenario: str, entrada: EntradaDeEscenario) -> ResumenDeEscenario:
    if escenario == "clientes-y-pedidos":
        return await clientes_y_pedidos(tenant_id, entrada)
    if escenario == "planes-de-suscripcion":
        return await planes_de_suscripcion(tenant_id, entrada)
    raise EscenarioNoDisponible(f"Comercio no tiene el escenario {escenario}.")
