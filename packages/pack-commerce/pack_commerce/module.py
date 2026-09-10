"""CRMModule: el BaseModule del plugin.

Registra skills (catalog, order, lead), guardrail de montos, eventos y el
panel admin del CRM (Leads, Productos, Categorías, Promociones, Pedidos).

El pipeline de venta es: lead -> cotización (con promoción) -> pedido.
No hay deals.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from core.db.session import get_session
from core.mcp.types import BaseModule, GuardrailResult
from core.shared.events import ON_CONVERSATION_STARTED
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_commerce.config import CRMConfig
from pack_commerce.models import (
    Category,
    CategoryComplement,
    Inventory,
    Lead,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    Promotion,
    Quote,
)
from pack_commerce.skills import CatalogSkill, LeadSkill, OrderSkill, classify_customer

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

log = get_logger(__name__)

PLUGIN_ID = "pack_commerce"

MONEY_IN_TEXT = re.compile(r"\$\s?[\d.,]+")

admin_router = APIRouter(prefix="/crm", tags=[PLUGIN_ID])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_CORE_TEMPLATES = Path(__file__).resolve().parents[3] / "core" / "admin" / "templates"
templates = Jinja2Templates(directory=[str(_TEMPLATES_DIR), str(_CORE_TEMPLATES)])


# ------------------------------------------------------------------ helpers
def _render(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200):  # type: ignore[no-untyped-def]
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _money(value: Any) -> str:
    return f"${float(value or 0):,.0f}"


def _iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


async def _require_admin(request: Request) -> dict[str, Any] | None:
    """Contexto común del panel (incluye el selector de tenants) o None si no hay sesión.

    Reutiliza `core.admin.context.base_context` en vez de construir un contexto
    parcial: así todas las rutas del pack reciben `tenants`, `active_tenant_id` y
    `flash`, igual que las páginas del core. Los packs dependen del core.
    """
    from core.admin.context import base_context

    return await base_context(request, "crm")


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


# ------------------------------------------------------------------ dashboard
@admin_router.get("", response_class=HTMLResponse)
@admin_router.get("/", response_class=HTMLResponse)
async def crm_dashboard(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        leads = (await session.execute(select(Lead))).scalars().all()
        products = (await session.execute(select(Product))).scalars().all()
        promotions = (await session.execute(select(Promotion))).scalars().all()
        orders = (await session.execute(select(Order))).scalars().all()
    ctx.update(
        kpi={
            "leads": len(leads),
            "productos": len(products),
            "promociones": len(promotions),
            "pedidos": len(orders),
        },
        recent_orders=sorted(orders, key=lambda o: o.created_at, reverse=True)[:5],
        _money=_money,
        _iso=_iso,
    )
    return _render(request, "crm/dashboard.html", ctx)


# ------------------------------------------------------------------ leads (vista por descriptor, Fase D)
async def _leads_list() -> list[dict[str, Any]]:
    """Filas para el renderizador por descriptor (Sprint 4, Fase D).

    Todo `Customer` del tenant ES un lead: no hace falta crear ni sincronizar
    nada (mismo diseño que agent-sale, ADR-036 "clasificación de cliente en 5
    niveles y rediseño de leads") — apenas alguien escribe por primera vez ya
    existe su `Customer` (`core/orchestrator/consumer.py::_resolve_conversation`),
    y `estado`/`segmento` se calculan en vivo contra su actividad real, sin
    depender de que el LLM llame ninguna tool. `pack_commerce_leads` sigue
    existiendo solo como enriquecimiento opcional de nombre/interés (lo que el
    LLM o un admin haya capturado en la conversación).

    Filtro explícito por tenant (defensa en profundidad, además de RLS): el panel
    es multi-tenant y una policy caída no debe convertir esto en una fuga."""
    from core.db.models import Conversation, Customer, Escalation, Message

    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        customers = (
            await session.execute(
                select(Customer)
                .where(Customer.tenant_id == tenant_id, Customer.deleted_at.is_(None))
                .order_by(Customer.created_at.desc())
                .limit(200)
            )
        ).scalars().all()

        order_customer_ids = set(
            (
                await session.execute(
                    select(Order.customer_id).where(Order.tenant_id == tenant_id).distinct()
                )
            )
            .scalars()
            .all()
        )
        quote_customer_ids = set(
            (
                await session.execute(
                    select(Quote.customer_id).where(Quote.tenant_id == tenant_id).distinct()
                )
            )
            .scalars()
            .all()
        )
        escalated_customer_ids = set(
            (
                await session.execute(
                    select(Conversation.customer_id)
                    .join(Escalation, Escalation.conversation_id == Conversation.id)
                    .where(Conversation.tenant_id == tenant_id)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        order_stats = {
            row.customer_id: (row.pedidos, row.ultima_compra)
            for row in (
                await session.execute(
                    select(
                        Order.customer_id,
                        func.count().label("pedidos"),
                        func.max(Order.created_at).label("ultima_compra"),
                    )
                    .where(Order.tenant_id == tenant_id, Order.status != "expirado")
                    .group_by(Order.customer_id)
                )
            ).all()
        }
        last_message_at = {
            row.customer_id: row.ultimo_mensaje
            for row in (
                await session.execute(
                    select(
                        Conversation.customer_id,
                        func.max(Message.created_at).label("ultimo_mensaje"),
                    )
                    .join(Message, Message.conversation_id == Conversation.id)
                    .where(Conversation.tenant_id == tenant_id)
                    .group_by(Conversation.customer_id)
                )
            ).all()
        }
        leads_by_customer = {
            lead.customer_id: lead
            for lead in (
                await session.execute(select(Lead).where(Lead.tenant_id == tenant_id))
            )
            .scalars()
            .all()
            if lead.customer_id is not None
        }

    rows: list[dict[str, Any]] = []
    for c in customers:
        lead = leads_by_customer.get(c.id)
        if c.id in order_customer_ids:
            estado = "con_pedido"
        elif c.id in escalated_customer_ids:
            estado = "escalada"
        elif c.id in quote_customer_ids:
            estado = "con_cotizacion"
        else:
            estado = "sin_actividad_comercial"
        pedidos, _ultima_compra = order_stats.get(c.id, (0, None))
        activity = last_message_at.get(c.id) or c.created_at
        rows.append(
            {
                "_id": str(c.id),
                "name": (lead.name if lead else None) or c.name or c.external_id,
                "interest": (lead.interest if lead else "") or "",
                "segment": await classify_customer(c.id),
                "estado": estado,
                "pedidos": pedidos,
                "last_activity": _iso(activity) if activity else "",
            }
        )
    rows.sort(key=lambda r: r["last_activity"], reverse=True)
    return rows


async def _leads_save(record_id: str | None, values: dict[str, Any]) -> None:
    """`record_id` es el `customer.id` (no un `lead.id`): estado/segmento ya se
    calculan solos, lo único editable a mano es el enriquecimiento de
    nombre/interés — se crea la fila de `Lead` si todavía no existía."""
    if record_id is None:
        return
    try:
        customer_uuid = uuid.UUID(record_id)
    except ValueError:
        return
    from core.db.models import Customer

    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        # customer_id es FK lógica (sin constraint física) — validar que el
        # customer exista y sea de este tenant antes de crear/tocar el Lead,
        # para no dejar una referencia colgante hacia otro tenant.
        customer = (
            await session.execute(
                select(Customer).where(
                    Customer.id == customer_uuid, Customer.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if customer is None:
            return

        lead = (
            await session.execute(select(Lead).where(Lead.customer_id == customer_uuid))
        ).scalar_one_or_none()
        if lead is None:
            lead = Lead(customer_id=customer_uuid, idempotency_key=f"manual:{customer_uuid}")
            session.add(lead)
        if "name" in values:
            lead.name = str(values["name"]) or None
        if "interest" in values:
            lead.interest = str(values["interest"] or "")
        lead.last_activity_at = datetime.now(UTC)


# ------------------------------------------------------------------ productos
@admin_router.get("/productos", response_class=HTMLResponse)
async def crm_products(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        products = (await session.execute(select(Product).order_by(Product.name))).scalars().all()
        variants = (await session.execute(select(ProductVariant))).scalars().all()
        inventory = (await session.execute(select(Inventory))).scalars().all()
        categories = (await session.execute(select(Category).order_by(Category.name))).scalars().all()
    cat_by_id = {c.id: c for c in categories}
    inv_by_variant = {i.variant_id: i for i in inventory}
    variant_by_product: dict[str, list[ProductVariant]] = {}
    for v in variants:
        variant_by_product.setdefault(str(v.product_id), []).append(v)
    ctx.update(
        products=[
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "category": cat_by_id[p.category_id].name if p.category_id in cat_by_id else "Sin categoría",
                "price_raw": float((variant_by_product.get(str(p.id)) or [None])[0].price or 0),
                "variants": [
                    {
                        "id": v.id,
                        "sku": v.sku,
                        "attributes": v.attributes or {},
                        "price": _money(v.price),
                        "stock": inv_by_variant[v.id].stock_quantity if v.id in inv_by_variant else 0,
                        "active": v.active,
                    }
                    for v in variant_by_product.get(str(p.id), [])
                ],
            }
            for p in products
        ],
        categories=[{"id": c.id, "name": c.name} for c in categories],
        _money=_money,
    )
    return _render(request, "crm/productos.html", ctx)


@admin_router.post("/productos/guardar")
async def product_save(
    request: Request,
    product_id: str = Form(default=""),
    name: str = Form(default=""),
    description: str = Form(default=""),
    category_id: str = Form(default=""),
    sku: str = Form(default=""),
    price: float = Form(default=0),
    stock: int = Form(default=0),
):
    if await _require_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        if product_id:
            product = await session.get(Product, product_id)
            if product is not None:
                product.name = name
                product.description = description
                product.category_id = uuid.UUID(category_id) if category_id else None
                variant = (
                    await session.execute(
                        select(ProductVariant).where(ProductVariant.product_id == product.id)
                    )
                ).scalars().first()
                if variant is not None:
                    variant.sku = sku or variant.sku
                    variant.price = price or variant.price
                    inv = (
                        await session.execute(
                            select(Inventory).where(Inventory.variant_id == variant.id)
                        )
                    ).scalar_one_or_none()
                    if inv is None:
                        session.add(Inventory(variant_id=variant.id, stock_quantity=stock))
                    else:
                        inv.stock_quantity = stock
        else:
            product = Product(name=name, description=description)
            if category_id:
                product.category_id = uuid.UUID(category_id)
            session.add(product)
            await session.flush()
            variant = ProductVariant(
                product_id=product.id, sku=sku or f"SKU-{product.id.hex[:8]}", price=price
            )
            session.add(variant)
            await session.flush()
            session.add(Inventory(variant_id=variant.id, stock_quantity=stock))
            product.has_variants = False
    return RedirectResponse("/crm/productos?flash=Producto guardado", status_code=303)


@admin_router.post("/productos/importar-csv")
async def product_import_csv(request: Request, file: Annotated[UploadFile, File()]):
    if await _require_admin(request) is None:
        return _login_redirect()
    content = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    if not rows or "name" not in rows[0] or "price" not in rows[0]:
        return RedirectResponse(
            "/crm/productos?flash=CSV inválido: requiere columnas name y price&flash_type=danger",
            status_code=303,
        )
    created = 0
    async with get_session() as session:
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            sku = (row.get("sku") or "").strip() or None
            price = float(row.get("price") or 0)
            stock = int(float(row.get("stock") or 0))
            description = (row.get("description") or "").strip()
            cat_name = (row.get("category") or "").strip()
            product = Product(name=name, description=description)
            if cat_name:
                cat = (
                    await session.execute(select(Category).where(Category.name == cat_name))
                ).scalar_one_or_none()
                if cat is not None:
                    product.category_id = cat.id
            session.add(product)
            await session.flush()
            variant = ProductVariant(
                product_id=product.id, sku=sku or f"SKU-{product.id.hex[:8]}", price=price
            )
            session.add(variant)
            await session.flush()
            session.add(Inventory(variant_id=variant.id, stock_quantity=stock))
            created += 1
    return RedirectResponse(
        f"/crm/productos?flash={created} productos importados", status_code=303
    )


# ------------------------------------------------------------------ categorías
@admin_router.get("/categorias", response_class=HTMLResponse)
async def crm_categories(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        categories = (await session.execute(select(Category).order_by(Category.sort_order, Category.name))).scalars().all()
        complements = (await session.execute(select(CategoryComplement))).scalars().all()
    comp_by_cat: dict[str, list[str]] = {}
    for c in complements:
        comp_by_cat.setdefault(str(c.category_id), []).append(str(c.complementary_category_id))
    cat_by_id = {str(c.id): c for c in categories}

    def build(parent_id: uuid.UUID | None) -> list[dict[str, Any]]:
        nodes = []
        for c in categories:
            if c.parent_id == parent_id:
                nodes.append(
                    {
                        "id": c.id,
                        "parent_id": c.parent_id,
                        "name": c.name,
                        "sort_order": c.sort_order,
                        "active": c.active,
                        "children": build(c.id),
                        "complements": [cat_by_id[x].name for x in comp_by_cat.get(str(c.id), []) if x in cat_by_id],
                    }
                )
        return nodes

    ctx.update(tree=build(None), categories=categories, _money=_money)
    return _render(request, "crm/categorias.html", ctx)


@admin_router.post("/categorias/guardar")
async def category_save(
    request: Request,
    category_id: str = Form(default=""),
    name: str = Form(default=""),
    parent_id: str = Form(default=""),
    sort_order: int = Form(default=0),
    complements: str = Form(default=""),
):
    if await _require_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        parent = uuid.UUID(parent_id) if parent_id else None
        if category_id:
            category = await session.get(Category, category_id)
            if category is not None:
                if parent is not None:
                    # Una categoría no puede ser su propia padre ni un descendiente
                    # de sí misma (eso la dejaría inalcanzable en el árbol del listado).
                    ancestor_id: uuid.UUID | None = parent
                    seen: set[uuid.UUID] = set()
                    while ancestor_id is not None and ancestor_id not in seen:
                        if ancestor_id == category.id:
                            return RedirectResponse(
                                "/crm/categorias?flash=Una categoría no puede ser su propia"
                                " categoría padre ni descendiente de sí misma&flash_type=danger",
                                status_code=303,
                            )
                        seen.add(ancestor_id)
                        ancestor = await session.get(Category, ancestor_id)
                        ancestor_id = ancestor.parent_id if ancestor is not None else None
                category.name = name
                category.parent_id = parent
                category.sort_order = sort_order
        else:
            category = Category(name=name, parent_id=parent, sort_order=sort_order)
            session.add(category)
            await session.flush()

        if category_id:
            await session.execute(
                CategoryComplement.__table__.delete().where(CategoryComplement.category_id == category.id)
            )
        for raw in complements.split(","):
            comp = raw.strip()
            if not comp:
                continue
            try:
                comp_uuid = uuid.UUID(comp)
            except ValueError:
                continue
            if comp_uuid == category.id:
                continue
            session.add(CategoryComplement(category_id=category.id, complementary_category_id=comp_uuid))
    return RedirectResponse("/crm/categorias?flash=Categoría guardada", status_code=303)


# ------------------------------------------------------------------ promociones
@admin_router.get("/promociones", response_class=HTMLResponse)
async def crm_promotions(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        promotions = (await session.execute(select(Promotion).order_by(Promotion.created_at.desc()))).scalars().all()
        categories = (await session.execute(select(Category).order_by(Category.name))).scalars().all()
        products = (await session.execute(select(Product).order_by(Product.name))).scalars().all()
    ctx.update(
        promotions=[
            {
                "id": p.id,
                "type": p.type,
                "label": (p.rules or {}).get("label", ""),
                "discount_pct": (p.rules or {}).get("discount_pct", ""),
                "discount_raw": float((p.rules or {}).get("discount_pct", 0) or 0),
                "active": p.active,
                "valid_from": _iso(p.valid_from),
                "valid_to": _iso(p.valid_to),
                "segments": p.eligible_segments or [],
                "once_per_customer": bool((p.rules or {}).get("once_per_customer")),
            }
            for p in promotions
        ],
        categories=[{"id": c.id, "name": c.name} for c in categories],
        products=[{"id": p.id, "name": p.name} for p in products],
    )
    return _render(request, "crm/promociones.html", ctx)


@admin_router.post("/promociones/guardar")
async def promotion_save(
    request: Request,
    promotion_id: str = Form(default=""),
    name: str = Form(default=""),
    promo_type: str = Form(default="temporada"),
    discount_pct: float = Form(default=0),
    once_per_customer: str = Form(default=""),
    valid_from: str = Form(default=""),
    valid_to: str = Form(default=""),
    category_id: str = Form(default=""),
    product_id: str = Form(default=""),
    segments: str = Form(default=""),
):
    if await _require_admin(request) is None:
        return _login_redirect()
    rules: dict[str, Any] = {"label": name, "discount_pct": discount_pct}
    if promo_type == "campaña" and once_per_customer == "on":
        rules["once_per_customer"] = True
    segments_list = [s.strip() for s in segments.split(",") if s.strip()] or None

    def parse_dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    async with get_session() as session:
        if promotion_id:
            promo = await session.get(Promotion, promotion_id)
            if promo is not None:
                promo.type = promo_type
                promo.rules = rules
                promo.valid_from = parse_dt(valid_from)
                promo.valid_to = parse_dt(valid_to)
                promo.category_id = uuid.UUID(category_id) if category_id else None
                promo.product_id = uuid.UUID(product_id) if product_id else None
                promo.eligible_segments = segments_list
        else:
            promo = Promotion(
                type=promo_type,
                rules=rules,
                valid_from=parse_dt(valid_from),
                valid_to=parse_dt(valid_to),
                category_id=uuid.UUID(category_id) if category_id else None,
                product_id=uuid.UUID(product_id) if product_id else None,
                eligible_segments=segments_list,
            )
            session.add(promo)
    return RedirectResponse("/crm/promociones?flash=Promoción guardada", status_code=303)


@admin_router.post("/promociones/{promotion_id}/toggle")
async def promotion_toggle(request: Request, promotion_id: uuid.UUID):
    if await _require_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        promo = await session.get(Promotion, promotion_id)
        if promo is not None:
            promo.active = not promo.active
    return RedirectResponse("/crm/promociones?flash=Promoción actualizada", status_code=303)


# ------------------------------------------------------------------ pedidos
@admin_router.get("/pedidos", response_class=HTMLResponse)
async def crm_orders(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        orders = (await session.execute(select(Order).order_by(Order.created_at.desc()).limit(200))).scalars().all()
        items = (await session.execute(select(OrderItem))).scalars().all()
        variants = (await session.execute(select(ProductVariant, Product).join(Product, Product.id == ProductVariant.product_id))).all()
    var_by_id = {v.id: p for v, p in variants}
    items_by_order: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        items_by_order.setdefault(str(i.order_id), []).append(
            {
                "name": var_by_id[i.variant_id].name if i.variant_id in var_by_id else "Producto",
                "quantity": i.quantity,
                "unit_price": _money(i.unit_price),
            }
        )
    ctx.update(
        orders=[
            {
                "id": o.id,
                "public_order_number": o.public_order_number,
                "total": _money(o.total),
                "total_raw": float(o.total),
                "status": o.status,
                "payment_status": o.payment_status,
                "payment_method": o.payment_method,
                "delivery_method": o.delivery_method,
                "delivery_address": o.delivery_address,
                "delivery_full_name": o.delivery_full_name,
                "created_at": _iso(o.created_at),
                "line_items": items_by_order.get(str(o.id), []),
            }
            for o in orders
        ],
        _money=_money,
    )
    return _render(request, "crm/pedidos.html", ctx)


@admin_router.post("/pedidos/{order_id}/estado")
async def order_update_status(request: Request, order_id: uuid.UUID, status: str = Form(...)):
    if await _require_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        order = await session.get(Order, order_id)
        if order is not None and status in ("abierto", "despachado", "entregado", "cancelado", "expirado"):
            order.status = status
            order.status_changed_at = datetime.now(UTC)
    return RedirectResponse("/crm/pedidos?flash=Estado actualizado", status_code=303)


# ------------------------------------------------------------------ módulo
class CRMModule(BaseModule):
    id = PLUGIN_ID
    name = "CRM"
    version = "2.0.0"
    description = "Leads, catálogo (menú), promociones y pedidos para el negocio"
    requires_config = True
    config_schema = CRMConfig
    admin_menu: list[dict[str, str]] = [  # noqa: RUF012
        {"label": "Dashboard", "path": "/crm/"},
        {"label": "Leads", "path": "/admin/packs/pack_commerce/leads"},
        {"label": "Productos", "path": "/crm/productos"},
        {"label": "Categorías", "path": "/crm/categorias"},
        {"label": "Promociones", "path": "/crm/promociones"},
        {"label": "Pedidos", "path": "/crm/pedidos"},
    ]
    admin_router = admin_router
    declared_skills: list[type] = [  # noqa: RUF012
        LeadSkill,
        CatalogSkill,
        OrderSkill,
    ]

    def on_activate(self, host: MCPHost) -> None:
        host.register_skill(LeadSkill())
        host.register_skill(CatalogSkill())
        host.register_skill(OrderSkill())
        host.register_guardrail("crm_amount_check", self._amount_guardrail, priority=5)

        # Vistas por descriptor (Fase D, Sprint 4): la del panel se sirve con HTML
        # genérico — cero plantillas/rutas propias del pack.
        self._register_views()

        # La suscripción se registra SIEMPRE: la decisión de auto_create_lead se
        # evalúa por tenant en el evento (la config activada no es global — vive
        # en plugin_configs y un tenant puede desactivarla sin afectar al resto).
        host.subscribe(ON_CONVERSATION_STARTED, self._on_conversation_started)

        # importar jobs.py registra los @register_cron en Celery Beat
        import pack_commerce.jobs  # noqa: F401

    def _register_views(self) -> None:
        from core.admin.view_registry import (
            TableColumn,
            TableDescriptor,
            TableFilter,
            ViewDescriptor,
            view_registry,
        )

        view_registry.register(
            PLUGIN_ID,
            ViewDescriptor(
                title="Leads",
                path="leads",
                table=TableDescriptor(
                    columns=[
                        TableColumn(key="name", label="Lead"),
                        TableColumn(key="interest", label="Interés"),
                        TableColumn(key="segment", label="Clasificación"),
                        TableColumn(key="estado", label="Estado"),
                        TableColumn(key="pedidos", label="Pedidos"),
                        TableColumn(key="last_activity", label="Última actividad"),
                    ],
                    filters=[
                        TableFilter(
                            key="estado",
                            label="Estado",
                            options=[
                                {"value": "con_pedido", "label": "Con pedido"},
                                {"value": "escalada", "label": "Escalada"},
                                {"value": "con_cotizacion", "label": "Con cotización"},
                                {"value": "sin_actividad_comercial", "label": "Sin actividad"},
                            ],
                        ),
                        TableFilter(
                            key="segment",
                            label="Clasificación",
                            options=[
                                {"value": "nuevo", "label": "Nuevo"},
                                {"value": "ocasional", "label": "Ocasional"},
                                {"value": "frecuente", "label": "Frecuente"},
                                {"value": "fiel", "label": "Fiel"},
                                {"value": "inactivo", "label": "Inactivo"},
                            ],
                        ),
                    ],
                    page_size=20,
                ),
                form={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "title": "Nombre"},
                        "interest": {"type": "string", "title": "Interés"},
                    },
                },
            ),
            list_handler=_leads_list,
            save_handler=_leads_save,
        )

    # ------------------------------------------------------------ eventos
    async def _on_conversation_started(self, conversation_id: uuid.UUID, customer_id: uuid.UUID) -> None:
        """Crea un lead borrador cuando arranca una conversación nueva.

        Solo si el pack está habilitado para el tenant (ADR-0019) y su config
        tiene auto_create_lead activo (decisión por tenant, no global): sin esto,
        un tenant sin el pack o con la config apagada recibiría leads fantasma.
        """
        from core.agents.spec import enabled_packs, load_active_spec
        from core.plugin_manager import plugin_manager
        from core.shared.tenant import get_current_tenant_id

        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return
        spec = await load_active_spec(tenant_id)
        if self.id not in enabled_packs(spec):
            return
        config = await plugin_manager.load_config(self.id)
        if config is None or not getattr(config, "auto_create_lead", True):
            return

        from sqlalchemy import select

        from pack_commerce.models import Lead

        async with get_session() as session:
            existing = (
                await session.execute(select(Lead).where(Lead.customer_id == customer_id))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Lead(
                        customer_id=customer_id,
                        conversation_id=conversation_id,
                        status="nuevo",
                        idempotency_key=f"auto:{conversation_id}",
                    )
                )
                log.info("lead_draft_created", conversation_id=str(conversation_id))

    # ------------------------------------------------------------ guardrail
    async def _amount_guardrail(
        self, response_text: str, tool_results: list[dict[str, Any]]
    ) -> GuardrailResult:
        """Si la respuesta menciona montos ($) que no salieron de una tool, bloquear.

        Mismo principio que el price guardrail de agent-sale: el LLM no inventa cifras.
        """
        amounts_in_text = MONEY_IN_TEXT.findall(response_text)
        if not amounts_in_text:
            return GuardrailResult(passed=True)

        known: set[str] = set()
        for result in tool_results:
            for value in result.values():
                if isinstance(value, (int, float)):
                    known.add(f"{value:g}")

        def normalize(amount: str) -> str:
            return amount.replace("$", "").replace(".", "").replace(",", "").strip()

        for amount in amounts_in_text:
            cleaned = normalize(amount)
            if cleaned and not any(cleaned == normalize(k) or cleaned in k for k in known):
                return GuardrailResult(
                    passed=False,
                    reason=f"Monto no verificado en la respuesta: {amount}",
                    retry_instruction=(
                        "Mencionaste un monto que no proviene de ninguna herramienta. "
                        "No cites cifras específicas salvo que una herramienta las haya devuelto."
                    ),
                )
        return GuardrailResult(passed=True)