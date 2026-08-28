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
from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import BaseModule, GuardrailResult
from core.shared.events import ON_CONVERSATION_STARTED
from core.shared.logger import get_logger
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
)
from pack_commerce.skills import CatalogSkill, LeadSkill, OrderSkill

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
    from core.admin.auth import current_admin
    from core.config import get_settings
    from core.db.models import AppSettings
    from core.plugin_manager import plugin_manager

    admin = await current_admin(request)
    if admin is None:
        return None
    async with get_session() as session:
        app_settings = (await session.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    return {
        "request": request,
        "section": "crm",
        "admin": admin,
        "assistant_name": app_settings.assistant_name if app_settings else get_settings().app_name,
        "plugin_menu": plugin_manager.active_admin_menu(),
    }


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


# ------------------------------------------------------------------ leads
@admin_router.get("/leads", response_class=HTMLResponse)
async def crm_leads(request: Request):
    ctx = await _require_admin(request)
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        leads = (
            await session.execute(
                select(Lead).order_by(Lead.last_activity_at.desc()).limit(200)
            )
        ).scalars().all()
    ctx.update(leads=leads, _iso=_iso, _money=_money)
    return _render(request, "crm/leads.html", ctx)


@admin_router.post("/leads/{lead_id}/guardar")
async def lead_save(request: Request, lead_id: uuid.UUID, score: int = Form(0), status: str = Form("")):
    if await _require_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        lead = await session.get(Lead, lead_id)
        if lead is not None:
            lead.score = max(0, min(10, score))
            if status in ("nuevo", "calificado", "contactado", "convertido", "perdido"):
                lead.status = status
            lead.last_activity_at = datetime.now(UTC)
    return RedirectResponse("/crm/leads?flash=Lead actualizado", status_code=303)


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
        {"label": "Leads", "path": "/crm/leads"},
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

        # La suscripción se registra SIEMPRE: la decisión de auto_create_lead se
        # evalúa por tenant en el evento (la config activada no es global — vive
        # en plugin_configs y un tenant puede desactivarla sin afectar al resto).
        host.subscribe(ON_CONVERSATION_STARTED, self._on_conversation_started)

        # importar jobs.py registra los @register_cron en Celery Beat
        import pack_commerce.jobs  # noqa: F401

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