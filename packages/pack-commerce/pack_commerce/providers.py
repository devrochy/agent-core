"""Contribución del pack a la vista global de clientes (ADR-0056, HU-UX-16).

Aporta al lead global del core la clasificación del cliente (nuevo, ocasional, frecuente, fiel o
inactivo, la misma regla que `skills.classify_customer`, de agent-sale) y el estado, puntaje e
interés de su último lead de venta. La clasificación se calcula en SQL para toda la página de
una vez (sin un `classify_customer` por fila) y la misma consulta sirve de filtro.

Desde la ficha se editan el estado y el interés de ese mismo lead (ADR-0149). Su nombre, teléfono
y correo no se muestran ni se editan aquí: los datos de contacto del cliente son los del perfil.

Importar este módulo (lo hace `module.py`) registra el provider.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, case, func, literal, select, union

from core.admin.lead_registry import (
    Cursor,
    LeadCampoEditable,
    LeadColumn,
    LeadEdicion,
    LeadFilter,
    LeadRelacion,
    TimelineEvent,
    antes_del_cursor,
    lead_registry,
)
from core.db.models import Customer
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_commerce.models import Lead, Order, Quote

PLUGIN_ID = "pack_commerce"

SEGMENTOS = (
    ("nuevo", "Nuevo"),
    ("ocasional", "Ocasional"),
    ("frecuente", "Frecuente"),
    ("fiel", "Fiel"),
    ("inactivo", "Inactivo"),
)
ESTADOS_LEAD = (
    ("nuevo", "Nuevo"),
    ("calificado", "Calificado"),
    ("contactado", "Contactado"),
    ("convertido", "Convertido"),
    ("perdido", "Perdido"),
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
        .where(Order.tenant_id == tid, Order.status != "expirado")
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
            con_pedidos = select(Order.customer_id).where(Order.tenant_id == tid, Order.status != "expirado")
            return select(Customer.id).where(Customer.tenant_id == tid, Customer.id.not_in(con_pedidos))
        seg = _segmentos_con_pedidos(tid, await _umbrales()).subquery()
        return select(seg.c.customer_id).where(seg.c.segmento == value)
    if key == "estado_lead":
        ultimo = _ultimo_lead(tid).subquery()
        return select(ultimo.c.customer_id).where(ultimo.c.status == value)
    return None


def _ultimo_lead(tid: uuid.UUID) -> Select[Any]:
    """El lead más reciente de cada cliente (por última actividad)."""
    return (
        select(Lead.customer_id, Lead.status, Lead.score, Lead.interest)
        .where(Lead.tenant_id == tid, Lead.customer_id.is_not(None))
        .distinct(Lead.customer_id)
        .order_by(Lead.customer_id, Lead.last_activity_at.desc())
    )


async def _resolver(customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not customer_ids:
        return {}
    tid = get_current_tenant_id()
    seg = _segmentos_con_pedidos(tid, await _umbrales()).subquery()
    ultimo = _ultimo_lead(tid).subquery()
    async with get_session() as session:
        segmentos = dict(
            (await session.execute(select(seg.c.customer_id, seg.c.segmento).where(seg.c.customer_id.in_(customer_ids)))).tuples().all()
        )
        leads = {
            row.customer_id: row
            for row in (await session.execute(select(ultimo).where(ultimo.c.customer_id.in_(customer_ids)))).all()
        }
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
    estados = dict(ESTADOS_LEAD)
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for cid in customer_ids:
        lead = leads.get(cid)
        compras = pedidos.get(cid)
        n = int(getattr(compras, "n", 0) or 0)
        total = float(getattr(compras, "total", 0) or 0)
        ultimo = getattr(compras, "ultimo", None)
        out[cid] = {
            "clasificacion": etiquetas[segmentos.get(cid, "nuevo")],
            "estado_lead": estados.get(lead.status, lead.status) if lead else None,
            "puntaje": lead.score if lead else None,
            "interes": (lead.interest or None) if lead else None,
            "pedidos": n,
            "total_compras": total,
            "ticket_promedio": round(total / n, 2) if n else 0.0,
            "ultimo_pedido": ultimo.strftime("%Y-%m-%d") if ultimo else None,
            "cotizaciones_abiertas": int(cotizaciones.get(cid, 0) or 0),
        }
    return out


async def _leer_lead(customer_id: uuid.UUID) -> dict[str, Any]:
    """Estado (código) e interés del lead que muestra la ficha (el de `_ultimo_lead`)."""
    ultimo = _ultimo_lead(get_current_tenant_id()).subquery()
    async with get_session() as session:
        lead = (
            await session.execute(select(ultimo).where(ultimo.c.customer_id == customer_id))
        ).first()
    if lead is None:
        return {}
    return {"estado_lead": lead.status, "interes": lead.interest or ""}


async def _escribir_lead(customer_id: uuid.UUID, valores: dict[str, str]) -> None:
    """Cambia el estado o el interés del lead que muestra la ficha; si el cliente no tiene lead,
    lo crea (como el formulario del panel SSR, `manual:<cliente>`) sin copiar datos de contacto.

    Solo un cambio de estado mueve `last_activity_at` (ADR-0149): marcar «calificado» a mano deja
    al lead en el seguimiento automático de `jobs.py`, corregir el interés no."""
    tid = get_current_tenant_id()
    async with get_session() as session:
        # El mismo bloqueo que la memoria (`facts.py`): dos ediciones a la vez no crean dos leads.
        cliente = (
            await session.execute(
                select(Customer.id)
                .where(Customer.tenant_id == tid, Customer.id == customer_id, Customer.deleted_at.is_(None))
                .with_for_update(key_share=True)
            )
        ).scalar_one_or_none()
        if cliente is None:
            return
        lead = (
            await session.execute(
                select(Lead)
                .where(Lead.tenant_id == tid, Lead.customer_id == customer_id)
                .order_by(Lead.last_activity_at.desc(), Lead.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if lead is None:
            lead = Lead(
                tenant_id=tid,
                customer_id=customer_id,
                interest="",
                status="nuevo",
                idempotency_key=f"manual:{customer_id}",
            )
            session.add(lead)
        if "estado_lead" in valores and valores["estado_lead"] != lead.status:
            lead.status = valores["estado_lead"]
            lead.last_activity_at = datetime.now(UTC)
        if "interes" in valores:
            lead.interest = valores["interes"]


async def _con_datos(ids: list[uuid.UUID] | None) -> Select[Any]:
    """Clientes con pedido, cotización o lead de venta. No usa `_resolver`: ahí todo cliente trae
    «Nuevo» y 0 pedidos, que no son datos."""
    tid = get_current_tenant_id()
    fuentes = []
    for modelo in (Order, Quote, Lead):
        q = select(modelo.customer_id.label("customer_id")).where(
            modelo.tenant_id == tid, modelo.customer_id.is_not(None)
        )
        if ids is not None:
            q = q.where(modelo.customer_id.in_(ids))
        fuentes.append(q)
    return select(union(*fuentes).subquery().c.customer_id)


def _pesos(valor: Any) -> str:
    return f"${float(valor or 0):,.0f}".replace(",", ".")


async def _timeline(customer_id: uuid.UUID, cursor: Cursor | None, limit: int) -> list[TimelineEvent]:
    """Cotizaciones y pedidos del cliente (HU-CRM-04). Del pedido se ve la creación y su último
    cambio de estado (`status_changed_at`): no hay historia de estados guardada."""
    tid = get_current_tenant_id()
    eventos: list[TimelineEvent] = []
    async with get_session() as session:
        cotizaciones = (
            await session.execute(
                select(Quote)
                .where(
                    Quote.tenant_id == tid,
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
        LeadColumn(key="estado_lead", label="Estado del lead"),
        LeadColumn(key="puntaje", label="Puntaje"),
        LeadColumn(key="interes", label="Interés"),
        LeadColumn(key="pedidos", label="Pedidos"),
        LeadColumn(key="total_compras", label="Total comprado"),
        LeadColumn(key="ticket_promedio", label="Ticket promedio"),
        LeadColumn(key="ultimo_pedido", label="Último pedido"),
        LeadColumn(key="cotizaciones_abiertas", label="Cotizaciones abiertas"),
    ],
    resolver=_resolver,
    filters=[
        LeadFilter(key="clasificacion", label="Clasificación", options=SEGMENTOS),
        LeadFilter(key="estado_lead", label="Estado del lead", options=ESTADOS_LEAD),
    ],
    filter_query=_filter_query,
    timeline=_timeline,
    edicion=LeadEdicion(
        campos=(
            LeadCampoEditable(key="estado_lead", label="Estado del lead", tipo="opciones", options=ESTADOS_LEAD),
            LeadCampoEditable(key="interes", label="Interés", tipo="texto", max_length=500),
        ),
        leer=_leer_lead,
        escribir=_escribir_lead,
    ),
    agent_key="ventas",
    agent_name="Ventas",
    icon="shopping-cart",
    relacion=LeadRelacion(con_datos=_con_datos),
)


# Registra también los KPIs del módulo para el Resumen por agente (ADR-0171).
from pack_commerce import resumen as _resumen  # noqa: E402, F401
