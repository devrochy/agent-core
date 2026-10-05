"""API admin del pack de Comercio (ADR-0079).

Montada por el core en `/api/v1/pack/commerce`. Usa las dependencias del core
(`core.api.deps`) para heredar la identidad de admin, el `X-Tenant-Id` validado y
el RBAC de la sección `pack_commerce`; el core no conoce el dominio.

Cubre el catálogo (productos con su variante e inventario), categorías,
promociones y pedidos, con la misma semántica que el panel SSR del pack.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.session import get_owned, get_session

# Registra los providers de búsqueda global del pack (ADR-0096).
from . import search as _search  # noqa: F401
from . import sync as _sync  # noqa: F401
from .models import Category, Inventory, Order, OrderItem, Product, ProductVariant, Promotion

router = APIRouter(prefix="/api/v1/pack/commerce", tags=["pack · comercio"])

ORDER_STATUSES = ("abierto", "despachado", "entregado", "cancelado", "expirado")


# ------------------------------------------------------------------ dashboard
class CommerceKpi(BaseModel):
    leads: int
    products: int
    promotions: int
    orders: int


class RecentOrder(BaseModel):
    id: uuid.UUID
    public_order_number: str
    total: float
    status: str
    created_at: datetime | None = None


class CommerceDashboard(BaseModel):
    kpi: CommerceKpi
    recent_orders: list[RecentOrder]


@router.get("/dashboard", response_model=CommerceDashboard)
async def commerce_dashboard(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
) -> CommerceDashboard:
    from core.db.models import Customer

    tenant_id = principal.tenant_id
    async with get_session() as session:
        leads = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id, Customer.deleted_at.is_(None)
                )
            )
        ).scalars().all()
        products = (
            await session.execute(select(Product).where(Product.tenant_id == tenant_id))
        ).scalars().all()
        promotions = (
            await session.execute(select(Promotion).where(Promotion.tenant_id == tenant_id))
        ).scalars().all()
        orders = (
            await session.execute(
                select(Order)
                .where(Order.tenant_id == tenant_id)
                .order_by(Order.created_at.desc())
                .limit(5)
            )
        ).scalars().all()

    return CommerceDashboard(
        kpi=CommerceKpi(
            leads=len(leads),
            products=len(products),
            promotions=len(promotions),
            orders=len(orders),
        ),
        recent_orders=[
            RecentOrder(
                id=o.id,
                public_order_number=o.public_order_number,
                total=float(o.total or 0),
                status=o.status,
                created_at=o.created_at,
            )
            for o in orders
        ],
    )


# ------------------------------------------------------------------ productos
class VariantOut(BaseModel):
    id: uuid.UUID
    sku: str
    attributes: dict[str, Any]
    price: float
    stock: int
    active: bool


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    category_id: uuid.UUID | None
    category: str
    price: float
    variants: list[VariantOut]


class ProductSave(BaseModel):
    product_id: uuid.UUID | None = None
    name: str
    description: str = ""
    category_id: uuid.UUID | None = None
    sku: str = ""
    price: float = 0
    stock: int = 0


@router.get("/products", response_model=list[ProductOut])
async def list_products(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
) -> list[ProductOut]:
    tenant_id = principal.tenant_id
    async with get_session() as session:
        products = (
            (
                await session.execute(
                    select(Product).where(Product.tenant_id == tenant_id).order_by(Product.name)
                )
            )
            .scalars()
            .all()
        )
        variants = (
            (
                await session.execute(
                    select(ProductVariant).where(ProductVariant.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        inventory = (
            (
                await session.execute(
                    select(Inventory).where(Inventory.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        categories = (
            (
                await session.execute(
                    select(Category).where(
                        Category.deleted_at.is_(None), Category.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )

    cat_by_id = {c.id: c.name for c in categories}
    inv_by_variant = {i.variant_id: i.stock_quantity for i in inventory}
    variants_by_product: dict[str, list[ProductVariant]] = {}
    for v in variants:
        variants_by_product.setdefault(str(v.product_id), []).append(v)

    out: list[ProductOut] = []
    for p in products:
        product_variants = variants_by_product.get(str(p.id), [])
        out.append(
            ProductOut(
                id=p.id,
                name=p.name,
                description=p.description or "",
                category_id=p.category_id,
                category=cat_by_id.get(p.category_id, "Sin categoría"),
                price=float(product_variants[0].price or 0) if product_variants else 0.0,
                variants=[
                    VariantOut(
                        id=v.id,
                        sku=v.sku,
                        attributes=v.attributes or {},
                        price=float(v.price or 0),
                        stock=inv_by_variant.get(v.id, 0),
                        active=v.active,
                    )
                    for v in product_variants
                ],
            )
        )
    return out


@router.post("/products", status_code=204)
async def save_product(
    payload: ProductSave,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> Response:
    """Alta/edición del producto con su variante por defecto e inventario."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        # Defensa en profundidad: no dejar la categoría colgando a otro tenant.
        resolved_category_id: uuid.UUID | None = None
        if payload.category_id is not None:
            category = await get_owned(session, Category, payload.category_id, tenant_id)
            if category is not None:
                resolved_category_id = category.id

        if payload.product_id is not None:
            product = await get_owned(session, Product, payload.product_id, tenant_id)
            if product is None:
                raise ApiError.not_found("Producto no encontrado.")
            product.name = payload.name
            product.description = payload.description
            product.category_id = resolved_category_id
            variant = (
                (
                    await session.execute(
                        select(ProductVariant).where(
                            ProductVariant.product_id == product.id,
                            ProductVariant.tenant_id == tenant_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if variant is not None:
                if payload.sku:
                    variant.sku = payload.sku
                if payload.price:
                    variant.price = payload.price
                inventory = (
                    await session.execute(
                        select(Inventory).where(
                            Inventory.variant_id == variant.id,
                            Inventory.tenant_id == tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if inventory is None:
                    session.add(Inventory(variant_id=variant.id, stock_quantity=payload.stock))
                else:
                    inventory.stock_quantity = payload.stock
        else:
            product = Product(
                name=payload.name,
                description=payload.description or "",
                category_id=resolved_category_id,
            )
            session.add(product)
            await session.flush()
            variant = ProductVariant(
                product_id=product.id,
                sku=payload.sku or f"SKU-{product.id.hex[:8]}",
                price=payload.price,
            )
            session.add(variant)
            await session.flush()
            session.add(Inventory(variant_id=variant.id, stock_quantity=payload.stock))
            product.has_variants = False
    return Response(status_code=204)


# ------------------------------------------------------------------ categorías
class CategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    sort_order: int
    active: bool


class CategorySave(BaseModel):
    category_id: uuid.UUID | None = None
    name: str
    parent_id: uuid.UUID | None = None
    sort_order: int = 0


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
) -> list[CategoryOut]:
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Category)
                    .where(
                        Category.tenant_id == principal.tenant_id,
                        Category.deleted_at.is_(None),
                    )
                    .order_by(Category.sort_order, Category.name)
                )
            )
            .scalars()
            .all()
        )
    return [
        CategoryOut(
            id=c.id,
            name=c.name,
            parent_id=c.parent_id,
            sort_order=c.sort_order or 0,
            active=bool(c.active),
        )
        for c in rows
    ]


@router.post("/categories", status_code=204)
async def save_category(
    payload: CategorySave,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> Response:
    tenant_id = principal.tenant_id
    async with get_session() as session:
        resolved_parent: uuid.UUID | None = None
        if payload.parent_id is not None:
            parent = await get_owned(session, Category, payload.parent_id, tenant_id)
            if parent is not None:
                resolved_parent = parent.id

        if payload.category_id is not None:
            category = await get_owned(session, Category, payload.category_id, tenant_id)
            if category is None:
                raise ApiError.not_found("Categoría no encontrada.")
            category.name = payload.name
            category.parent_id = resolved_parent
            category.sort_order = payload.sort_order
        else:
            session.add(
                Category(
                    name=payload.name,
                    parent_id=resolved_parent,
                    sort_order=payload.sort_order,
                )
            )
    return Response(status_code=204)


@router.post("/categories/{category_id}/delete", status_code=204)
async def delete_category(
    category_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> Response:
    """Soft-delete (invariante 8): marca `deleted_at`."""
    from core.db.models import utcnow

    async with get_session() as session:
        category = await get_owned(session, Category, category_id, principal.tenant_id)
        if category is None:
            raise ApiError.not_found("Categoría no encontrada.")
        category.deleted_at = utcnow()
    return Response(status_code=204)


# ------------------------------------------------------------------ promociones
PROMOTION_TYPES = ("temporada", "volumen", "campaña")
# Mismos segmentos que `skills.classify_customer` (ADR-036).
PROMOTION_SEGMENTS = ("nuevo", "ocasional", "frecuente", "fiel", "inactivo")


class PromotionOut(BaseModel):
    id: uuid.UUID
    type: str
    rules: dict[str, Any]
    active: bool
    valid_from: datetime | None
    valid_to: datetime | None
    category_id: uuid.UUID | None
    product_id: uuid.UUID | None
    variant_id: uuid.UUID | None = None
    include_child_categories: bool = True
    eligible_segments: list[str] | None
    created_at: datetime | None = None


class PromotionSave(BaseModel):
    promotion_id: uuid.UUID | None = None
    type: str
    rules: dict[str, Any] = {}
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    category_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    variant_id: uuid.UUID | None = None
    include_child_categories: bool = True
    eligible_segments: list[str] | None = None
    active: bool | None = None


def _validate_promotion(payload: PromotionSave) -> None:
    """Validación semántica de la promoción.

    El formulario clásico solo validaba `required` y rangos HTML: una promoción
    de tipo `volumen` sin escalones, o con descuento fuera de rango, quedaba
    guardada y el motor la ignoraba en silencio.
    """
    if payload.type not in PROMOTION_TYPES:
        raise ApiError.bad_request(
            f"Tipo de promoción inválido: {payload.type}", "invalid_type"
        )
    if payload.valid_from and payload.valid_to and payload.valid_to <= payload.valid_from:
        raise ApiError.bad_request(
            "La vigencia termina antes de empezar.", "invalid_dates"
        )
    reglas = payload.rules or {}
    if not isinstance(reglas, dict):
        raise ApiError.bad_request("Las reglas deben ser un objeto.", "invalid_rules")
    pct = reglas.get("discount_pct")
    if pct is not None:
        try:
            pct_num = float(pct)
        except (TypeError, ValueError) as exc:
            raise ApiError.bad_request("El descuento debe ser un número.", "invalid_discount") from exc
        if not 0 <= pct_num <= 100:
            raise ApiError.bad_request(
                "El descuento debe estar entre 0 y 100.", "invalid_discount"
            )
    tiers = reglas.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, list) or not tiers:
            raise ApiError.bad_request(
                "Los escalones de volumen deben ser una lista con al menos uno.",
                "invalid_tiers",
            )
        for tier in tiers:
            if not isinstance(tier, dict):
                raise ApiError.bad_request("Escalón de volumen inválido.", "invalid_tiers")
            try:
                minimo = int(tier.get("min"))
            except (TypeError, ValueError) as exc:
                raise ApiError.bad_request(
                    "Cada escalón necesita una cantidad mínima.", "invalid_tiers"
                ) from exc
            if minimo < 1:
                raise ApiError.bad_request(
                    "La cantidad mínima del escalón debe ser 1 o más.", "invalid_tiers"
                )
            if tier.get("max") is not None:
                try:
                    maximo = int(tier["max"])
                except (TypeError, ValueError) as exc:
                    raise ApiError.bad_request("Cantidad máxima inválida.", "invalid_tiers") from exc
                if maximo < minimo:
                    raise ApiError.bad_request(
                        "El máximo del escalón no puede ser menor que el mínimo.", "invalid_tiers"
                    )
            tpct = tier.get("discount_pct")
            if tpct is None and not str(tier.get("free_item_sku") or "").strip():
                raise ApiError.bad_request(
                    "Cada escalón necesita un descuento o un artículo de regalo.",
                    "invalid_tiers",
                )
            if tpct is not None:
                try:
                    tpct_num = float(tpct)
                except (TypeError, ValueError) as exc:
                    raise ApiError.bad_request("Descuento del escalón inválido.", "invalid_tiers") from exc
                if not 0 <= tpct_num <= 100:
                    raise ApiError.bad_request(
                        "El descuento del escalón debe estar entre 0 y 100.", "invalid_tiers"
                    )
    if payload.eligible_segments is not None:
        desconocidos = [s for s in payload.eligible_segments if s not in PROMOTION_SEGMENTS]
        if desconocidos:
            raise ApiError.bad_request(
                f"Segmento desconocido: {', '.join(desconocidos)}", "invalid_segments"
            )


def _promotion_out(p: Promotion) -> PromotionOut:
    return PromotionOut(
        id=p.id,
        type=p.type,
        rules=p.rules or {},
        active=bool(p.active),
        valid_from=p.valid_from,
        valid_to=p.valid_to,
        category_id=p.category_id,
        product_id=p.product_id,
        variant_id=p.variant_id,
        include_child_categories=bool(getattr(p, "include_child_categories", True)),
        eligible_segments=list(p.eligible_segments) if p.eligible_segments else None,
        created_at=p.created_at,
    )


@router.get("/promotions", response_model=list[PromotionOut])
async def list_promotions(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
) -> list[PromotionOut]:
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Promotion)
                    .where(Promotion.tenant_id == principal.tenant_id)
                    .order_by(Promotion.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [_promotion_out(p) for p in rows]


@router.post("/promotions", status_code=204)
async def save_promotion(
    payload: PromotionSave,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> Response:
    tenant_id = principal.tenant_id
    _validate_promotion(payload)
    async with get_session() as session:
        resolved_category: uuid.UUID | None = None
        if payload.category_id is not None:
            category = await get_owned(session, Category, payload.category_id, tenant_id)
            if category is not None:
                resolved_category = category.id
        resolved_product: uuid.UUID | None = None
        if payload.product_id is not None:
            product = await get_owned(session, Product, payload.product_id, tenant_id)
            if product is not None:
                resolved_product = product.id

        if payload.promotion_id is not None:
            promotion = await get_owned(session, Promotion, payload.promotion_id, tenant_id)
            if promotion is None:
                raise ApiError.not_found("Promoción no encontrada.")
            promotion.type = payload.type
            promotion.rules = payload.rules
            promotion.valid_from = payload.valid_from
            promotion.valid_to = payload.valid_to
            promotion.category_id = resolved_category
            promotion.product_id = resolved_product
            promotion.variant_id = payload.variant_id
            promotion.include_child_categories = payload.include_child_categories
            promotion.eligible_segments = payload.eligible_segments
            if payload.active is not None:
                promotion.active = payload.active
        else:
            session.add(
                Promotion(
                    type=payload.type,
                    rules=payload.rules,
                    valid_from=payload.valid_from,
                    valid_to=payload.valid_to,
                    category_id=resolved_category,
                    product_id=resolved_product,
                    variant_id=payload.variant_id,
                    include_child_categories=payload.include_child_categories,
                    eligible_segments=payload.eligible_segments,
                    active=True if payload.active is None else payload.active,
                )
            )
    return Response(status_code=204)


@router.post("/promotions/{promotion_id}/toggle", status_code=204)
async def toggle_promotion(
    promotion_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> Response:
    async with get_session() as session:
        promotion = await get_owned(session, Promotion, promotion_id, principal.tenant_id)
        if promotion is None:
            raise ApiError.not_found("Promoción no encontrada.")
        promotion.active = not promotion.active
    return Response(status_code=204)


# ------------------------------------------------------------------ pedidos
class OrderLine(BaseModel):
    name: str
    quantity: int
    unit_price: float


class OrderOut(BaseModel):
    id: uuid.UUID
    public_order_number: str
    total: float
    status: str
    payment_status: str
    payment_method: str
    delivery_method: str
    delivery_full_name: str | None
    delivery_address: str | None
    created_at: datetime | None
    line_items: list[OrderLine]


class OrderStatusUpdate(BaseModel):
    status: str


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
) -> list[OrderOut]:
    tenant_id = principal.tenant_id
    async with get_session() as session:
        orders = (
            (
                await session.execute(
                    select(Order)
                    .where(Order.tenant_id == tenant_id)
                    .order_by(Order.created_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        items = (
            (
                await session.execute(
                    select(OrderItem).where(OrderItem.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        variants = (
            await session.execute(
                select(ProductVariant, Product)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(
                    ProductVariant.tenant_id == tenant_id, Product.tenant_id == tenant_id
                )
            )
        ).all()

    name_by_variant = {v.id: p.name for v, p in variants}
    items_by_order: dict[str, list[OrderLine]] = {}
    for i in items:
        items_by_order.setdefault(str(i.order_id), []).append(
            OrderLine(
                name=name_by_variant.get(i.variant_id, "Producto"),
                quantity=i.quantity,
                unit_price=float(i.unit_price or 0),
            )
        )

    return [
        OrderOut(
            id=o.id,
            public_order_number=o.public_order_number,
            total=float(o.total or 0),
            status=o.status,
            payment_status=o.payment_status,
            payment_method=o.payment_method,
            delivery_method=o.delivery_method,
            delivery_full_name=o.delivery_full_name,
            delivery_address=o.delivery_address,
            created_at=o.created_at,
            line_items=items_by_order.get(str(o.id), []),
        )
        for o in orders
    ]


@router.post("/orders/{order_id}/status", status_code=204)
async def update_order_status(
    order_id: uuid.UUID,
    payload: OrderStatusUpdate,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> Response:
    if payload.status not in ORDER_STATUSES:
        raise ApiError.bad_request(
            f"Estado inválido: {payload.status}. Válidos: {', '.join(ORDER_STATUSES)}",
            "invalid_status",
        )
    from datetime import UTC

    async with get_session() as session:
        order = await get_owned(session, Order, order_id, principal.tenant_id)
        if order is None:
            raise ApiError.not_found("Pedido no encontrado.")
        order.status = payload.status
        order.status_changed_at = datetime.now(UTC)
    return Response(status_code=204)


class ImportResult(BaseModel):
    created: int


@router.post("/products/import", response_model=ImportResult, status_code=201)
async def import_products_csv(
    file: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> ImportResult:
    """Importa productos desde un CSV (columnas `name` y `price`, paridad con el clásico)."""
    import csv
    import io

    contenido = (await file.read()).decode("utf-8", errors="ignore")
    filas = list(csv.DictReader(io.StringIO(contenido)))
    if not filas or "name" not in filas[0] or "price" not in filas[0]:
        raise ApiError.bad_request("CSV inválido: requiere columnas name y price", "invalid_csv")

    creados = 0
    async with get_session() as session:
        for fila in filas:
            nombre = str(fila.get("name") or "").strip()
            if not nombre:
                continue
            try:
                precio = Decimal(str(fila.get("price") or "0").replace(",", "").strip())
            except Exception:
                continue
            producto = Product(
                name=nombre,
                description=str(fila.get("description") or "").strip(),
                category_id=None,
            )
            session.add(producto)
            await session.flush()
            variante = ProductVariant(
                product_id=producto.id,
                sku=str(fila.get("sku") or "").strip() or f"SKU-{producto.id.hex[:8]}",
                price=precio,
            )
            session.add(variante)
            await session.flush()
            try:
                stock = int(str(fila.get("stock") or "0").strip() or 0)
            except ValueError:
                stock = 0
            session.add(Inventory(variant_id=variante.id, stock_quantity=stock))
            producto.has_variants = False
            creados += 1
    return ImportResult(created=creados)
