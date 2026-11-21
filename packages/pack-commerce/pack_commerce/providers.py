"""Contribución del pack a la vista global de clientes (ADR-0056, HU-UX-16).

Aporta al lead global del core la clasificación del cliente (nuevo, ocasional, frecuente, fiel o
inactivo, la misma regla que `skills.classify_customer`, de agent-sale), sus pedidos y sus cotizaciones. La
clasificación se calcula en SQL para toda la página de una vez y la misma consulta sirve de filtro.

El lead (etapa, puntaje, interés, seguimiento, motivo, valor) es del core desde ADR-0193: lo aporta
`core.leads.providers` y se edita allí.

Importar este módulo (lo hace `module.py`) registra el provider.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, and_, case, func, literal, select, union, union_all

from core.admin.lead_registry import (
    Cursor,
    LeadColumn,
    LeadFilter,
    LeadRelacion,
    TimelineEvent,
    antes_del_cursor,
    lead_registry,
)
from core.db.models import Customer
from core.db.session import get_session
from core.leads import servicio as leads_core
from core.shared.tenant import get_current_tenant_id
from pack_commerce import ficha as _ficha
from pack_commerce.models import Order, Quote

PLUGIN_ID = "pack_commerce"

SEGMENTOS = (
    ("nuevo", "Nuevo"),
    ("ocasional", "Ocasional"),
    ("frecuente", "Frecuente"),
    ("fiel", "Fiel"),
    ("inactivo", "Inactivo"),
)


async def _umbrales() -> dict[str, int]:
    """Umbrales del negocio con los mismos valores por defecto que `classify_customer`."""
    try:
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    return {
        "frecuente": getattr(config, "customer_frecuente_min_pedidos", 2) or 2,
        "fiel": getattr(config, "customer_fiel_min_pedidos", 5) or 5,
        "intervalo": getattr(config, "customer_frecuente_intervalo_max_dias", 45) or 45,
        "inactivo": getattr(config, "customer_inactivo_dias_sin_comprar", 120) or 120,
    }


def _dias(intervalo: Any) -> Any:
    """Días enteros de un `interval` (como `timedelta.days`)."""
    return func.floor(func.extract("epoch", intervalo) / 86400)


def _segmentos_con_pedidos(tid: uuid.UUID, u: dict[str, int]) -> Select[Any]:
    """`(customer_id, segmento)` de los clientes con al menos un pedido no expirado."""
    pedidos = (
        select(
            Order.customer_id,
            Order.created_at,
            func.lag(Order.created_at)
            .over(partition_by=Order.customer_id, order_by=Order.created_at)
            .label("anterior"),
        )
        .where(Order.tenant_id == tid, Order.deleted_at.is_(None), Order.status != "expirado")
        .subquery()
    )
    stats = (
        select(
            pedidos.c.customer_id,
            func.count().label("n"),
            func.max(pedidos.c.created_at).label("ultima"),
            func.max(_dias(pedidos.c.created_at - pedidos.c.anterior)).label("max_gap"),
        )
        .group_by(pedidos.c.customer_id)
        .subquery()
    )
    segmento = case(
        (_dias(func.now() - stats.c.ultima) > u["inactivo"], literal("inactivo")),
        (stats.c.n >= u["fiel"], literal("fiel")),
        (and_(stats.c.n >= u["frecuente"], stats.c.max_gap <= u["intervalo"]), literal("frecuente")),
        else_=literal("ocasional"),
    )
    return select(stats.c.customer_id, segmento.label("segmento"))


async def _filter_query(key: str, value: str) -> Select[Any] | None:
    tid = get_current_tenant_id()
    if key == "clasificacion":
        if value == "nuevo":
            con_pedidos = select(Order.customer_id).where(
            Order.tenant_id == tid, Order.deleted_at.is_(None), Order.status != "expirado"
        )
            return select(Customer.id).where(Customer.tenant_id == tid, Customer.id.not_in(con_pedidos))
        seg = _segmentos_con_pedidos(tid, await _umbrales()).subquery()
        return select(seg.c.customer_id).where(seg.c.segmento == value)
    return None


async def _facet_query(key: str) -> Select[Any] | None:
    """`(customer_id, valor)` de cada cliente para el filtro `key`, a lo sumo una fila por cliente.

    Es la misma regla que `_filter_query`, vista por cliente en vez de por valor, para que el core
    cuente las opciones con una sola consulta agrupada (HU-CRM-17).
    """
    tid = get_current_tenant_id()
    if key == "clasificacion":
        seg = _segmentos_con_pedidos(tid, await _umbrales()).subquery()
        con_pedidos = select(Order.customer_id).where(
            Order.tenant_id == tid, Order.deleted_at.is_(None), Order.status != "expirado"
        )
        return union_all(
            select(seg.c.customer_id.label("customer_id"), seg.c.segmento.label("valor")),
            select(Customer.id.label("customer_id"), literal("nuevo").label("valor")).where(
                Customer.tenant_id == tid, Customer.id.not_in(con_pedidos)
            ),
        )
    return None


async def _resolver(customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not customer_ids:
        return {}
    tid = get_current_tenant_id()
    seg = _segmentos_con_pedidos(tid, await _umbrales()).subquery()
    async with get_session() as session:
        segmentos = dict(
            (await session.execute(select(seg.c.customer_id, seg.c.segmento).where(seg.c.customer_id.in_(customer_ids)))).tuples().all()
        )
        pedidos = {
            row.customer_id: row
            for row in (
                await session.execute(
                    select(
                        Order.customer_id,
                        func.count(Order.id).label("n"),
                        func.coalesce(func.sum(Order.total), 0).label("total"),
                        func.max(Order.created_at).label("ultimo"),
                    )
                    .where(
                        Order.tenant_id == tid,
                        Order.deleted_at.is_(None),
                        Order.customer_id.in_(customer_ids),
                        Order.status != "expirado",
                    )
                    .group_by(Order.customer_id)
                )
            ).all()
        }
        cotizaciones = dict(
            (
                await session.execute(
                    select(Quote.customer_id, func.count(Quote.id))
                    .where(
                        Quote.tenant_id == tid,
                        Quote.deleted_at.is_(None),
                        Quote.customer_id.in_(customer_ids),
                        Quote.status.in_(("draft", "aceptada")),
                    )
                    .group_by(Quote.customer_id)
                )
            )
            .tuples()
            .all()
        )
    # Las columnas muestran etiquetas; los filtros usan los códigos (`filters[].options`).
    etiquetas = dict(SEGMENTOS)
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for cid in customer_ids:
        compras = pedidos.get(cid)
        n = int(getattr(compras, "n", 0) or 0)
        total = float(getattr(compras, "total", 0) or 0)
        ultimo = getattr(compras, "ultimo", None)
        out[cid] = {
            "clasificacion": etiquetas[segmentos.get(cid, "nuevo")],
            "pedidos": n,
            "total_compras": total,
            "ticket_promedio": round(total / n, 2) if n else 0.0,
            "ultimo_pedido": ultimo.strftime("%Y-%m-%d") if ultimo else None,
            "cotizaciones_abiertas": int(cotizaciones.get(cid, 0) or 0),
        }
    return out


async def _con_datos(ids: list[uuid.UUID] | None) -> Select[Any]:
    """Clientes con pedido, cotización o un lead con contenido. No usa `_resolver`: ahí todo cliente trae «Nuevo» y 0
    pedidos, que no son datos. El lead es del core (ADR-0193): se suma por su servicio, sin el modelo, para que el
    cliente que solo dejó sus datos siga siendo de Ventas (filtro `?agente=ventas`, botón de plantilla de la APK)."""
    tid = get_current_tenant_id()
    fuentes = []
    for modelo in (Order, Quote):
        q = select(modelo.customer_id.label("customer_id")).where(
            modelo.tenant_id == tid, modelo.deleted_at.is_(None), modelo.customer_id.is_not(None)
        )
        if ids is not None:
            q = q.where(modelo.customer_id.in_(ids))
        fuentes.append(q)
    fuentes.append(leads_core.clientes_con_datos(tid, ids))
    return select(union(*fuentes).subquery().c.customer_id)


def _pesos(valor: Any) -> str:
    return f"${float(valor or 0):,.0f}".replace(",", ".")


async def _timeline(customer_id: uuid.UUID, cursor: Cursor | None, limit: int) -> list[TimelineEvent]:
    """Cotizaciones y pedidos del cliente (HU-CRM-04). Del pedido se ve la creación y su último
    cambio de estado (`status_changed_at`): no hay historia de estados guardada. Los cambios de
    etapa del lead y sus seguimientos los pone el provider de leads del core (ADR-0193)."""
    tid = get_current_tenant_id()
    eventos: list[TimelineEvent] = []
    async with get_session() as session:
        cotizaciones = (
            await session.execute(
                select(Quote)
                .where(
                    Quote.tenant_id == tid,
                    Quote.deleted_at.is_(None),
                    Quote.customer_id == customer_id,
                    antes_del_cursor(Quote.created_at, Quote.id, "pack_commerce.cotizacion", cursor),
                )
                .order_by(Quote.created_at.desc(), Quote.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for q in cotizaciones:
            eventos.append(
                TimelineEvent(
                    at=q.created_at, source="pack_commerce.cotizacion", id=str(q.id), kind="cotizacion",
                    title=f"Cotización por {_pesos(q.total)}", detail=q.status,
                    link_kind="conversation", link_id=str(q.conversation_id),
                )
            )
        pedidos = (
            await session.execute(
                select(Order)
                .where(
                    Order.tenant_id == tid,
                    Order.deleted_at.is_(None),
                    Order.customer_id == customer_id,
                    antes_del_cursor(Order.created_at, Order.id, "pack_commerce.pedido", cursor),
                )
                .order_by(Order.created_at.desc(), Order.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for o in pedidos:
            eventos.append(
                TimelineEvent(
                    at=o.created_at, source="pack_commerce.pedido", id=str(o.id), kind="pedido",
                    title=f"Pedido {o.public_order_number} por {_pesos(o.total)}",
                    detail=f"Pago {o.payment_status}", link_kind="order", link_id=str(o.id),
                )
            )
        cambios = (
            await session.execute(
                select(Order)
                .where(
                    Order.tenant_id == tid,
                    Order.deleted_at.is_(None),
                    Order.customer_id == customer_id,
                    Order.status_changed_at.is_not(None),
                    antes_del_cursor(Order.status_changed_at, Order.id, "pack_commerce.pedido_estado", cursor),
                )
                .order_by(Order.status_changed_at.desc(), Order.id.desc())
                .limit(limit)
            )
        ).scalars().all()
        for o in cambios:
            assert o.status_changed_at is not None
            eventos.append(
                TimelineEvent(
                    at=o.status_changed_at, source="pack_commerce.pedido_estado", id=str(o.id), kind="pedido",
                    title=f"Pedido {o.public_order_number}: {o.status}", link_kind="order", link_id=str(o.id),
                )
            )
    return eventos


lead_registry.register(
    PLUGIN_ID,
    columns=[
        LeadColumn(key="clasificacion", label="Clasificación"),
        LeadColumn(key="pedidos", label="Pedidos"),
        LeadColumn(key="total_compras", label="Total comprado"),
        LeadColumn(key="ticket_promedio", label="Ticket promedio"),
        LeadColumn(key="ultimo_pedido", label="Último pedido"),
        LeadColumn(key="cotizaciones_abiertas", label="Cotizaciones abiertas"),
    ],
    resolver=_resolver,
    filters=[LeadFilter(key="clasificacion", label="Clasificación", options=SEGMENTOS)],
    filter_query=_filter_query,
    facet_query=_facet_query,
    timeline=_timeline,
    agent_key="ventas",
    agent_name="Ventas",
    icon="shopping-cart",
    relacion=LeadRelacion(con_datos=_con_datos, ficha=_ficha.ficha, detalle=_ficha.detalle),
)

# Registra también los KPIs del módulo para el Resumen por agente (ADR-0171).
from pack_commerce import datos as _datos  # noqa: E402, F401  (acciones de la política de datos)
from pack_commerce import resumen as _resumen  # noqa: E402, F401
