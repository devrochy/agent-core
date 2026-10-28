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
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update

from core import almacenamiento
from core.api.deps import Principal, get_principal, require
from core.api.errors import ApiError
from core.db.session import get_owned, get_session
from core.shared.tenant import tenant_slug

from . import aliados

# Registra los providers de búsqueda global del pack (ADR-0096).
from . import search as _search  # noqa: F401
from . import sync as _sync  # noqa: F401
from .fotos import key_foto_valida, url_foto
from .models import Ally, Category, Inventory, Order, OrderItem, Product, ProductVariant, Promotion

router = APIRouter(prefix="/api/v1/pack/commerce", tags=["pack · comercio"])

ORDER_STATUSES = ("abierto", "despachado", "entregado", "cancelado", "expirado")

# HU-CRM-01: la foto del producto es un link público. Solo https (WhatsApp la descarga
# por el link) y con tope de largo.
IMAGE_URL_MAX = 2048


def _image_url_valida(valor: str | None) -> str | None:
    """`None` si no viene; `""` para quitarla; si no, una URL https sin espacios."""
    if valor is None:
        return None
    valor = valor.strip()
    if not valor:
        return ""
    if not valor.lower().startswith("https://") or len(valor) <= len("https://") or any(c.isspace() for c in valor):
        raise ValueError("La foto tiene que ser un link https://")
    return valor


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
            await session.execute(
                select(Product).where(Product.tenant_id == tenant_id, Product.deleted_at.is_(None))
            )
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
    billing_period: str | None = None  # plan recurrente (HU-SUB-09)
    trial_days: int | None = None


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    category_id: uuid.UUID | None
    category: str
    ally_id: uuid.UUID | None = None  # HU-CRM-03
    price: float
    image_url: str | None = None  # resuelta: firmada si hay subida, si no el link (para mostrar)
    image_url_externa: str | None = None  # link externo crudo (HU-CRM-01), para el formulario
    image_key: str | None = None  # HU-CRM-05 (clave de la foto subida)
    variants: list[VariantOut]


class ProductSave(BaseModel):
    product_id: uuid.UUID | None = None
    name: str
    description: str = ""
    category_id: uuid.UUID | None = None
    # HU-CRM-03: si no viene, el aliado no cambia; `null` lo quita. Así un cliente que no
    # conoce los aliados (la APK) no los borra al editar.
    ally_id: uuid.UUID | None = None
    sku: str = ""
    # Precio, stock y días de prueba nunca negativos (422 con el campo marcado). Topes: lo que
    # cabe en la columna (`Numeric(12, 2)`, `Integer`) para no terminar en un 500 de la base.
    price: float = Field(default=0, ge=0, le=9_999_999_999, allow_inf_nan=False)
    stock: int = Field(default=0, ge=0, le=1_000_000_000)
    # HU-SUB-09: plan recurrente. None = no cambia; "" = no es recurrente.
    billing_period: Literal["mensual", "trimestral", "anual", ""] | None = None
    trial_days: int | None = Field(default=None, ge=0, le=365)
    # HU-CRM-01: None = no cambia; "" = quita la foto.
    image_url: str | None = Field(default=None, max_length=IMAGE_URL_MAX)
    # HU-CRM-05: clave de la foto subida (controlador único). None = no cambia; "" = quita.
    image_key: str | None = None

    @field_validator("image_url")
    @classmethod
    def _solo_https(cls, valor: str | None) -> str | None:
        return _image_url_valida(valor)


async def _validar_plan(tenant_id: uuid.UUID | None, payload: ProductSave, ya_recurrente: bool) -> None:
    """Un plan recurrente necesita un proveedor de suscripciones (HU-SUB-09) y solo
    un plan de $0 tiene prueba (D-16)."""
    if payload.trial_days and payload.price > 0:
        raise ApiError.bad_request("Solo el plan Free ($0) tiene días de prueba (D-16).", "prueba_en_plan_pago")
    if not payload.billing_period or ya_recurrente or tenant_id is None:
        return
    from core.subscriptions import estado_proveedor

    estado = await estado_proveedor(tenant_id)
    if estado["estado"] == "sin_proveedor":
        raise ApiError.bad_request(estado["mensaje"], "sin_proveedor_de_suscripciones")


def _key_producto_valida(key: str, slug: str | None) -> bool:
    """La clave de una foto subida tiene que ser de este negocio, sin `..` ni rutas raras, y
    existir en disco dentro de `TENANTS_ROOT/<slug>/productos/` (no basta el prefijo del texto)."""
    if not slug or not key_foto_valida(key, slug):
        return False
    return almacenamiento.abrir(key, dentro_de=almacenamiento.carpeta_de("producto", slug)) is not None


@router.get("/products", response_model=list[ProductOut])
async def list_products(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
    ally_id: Annotated[
        str | None,
        Query(description="HU-CRM-03: id del aliado, o `none` para los productos sin aliado."),
    ] = None,
) -> list[ProductOut]:
    tenant_id = principal.tenant_id
    filtro = select(Product).where(Product.tenant_id == tenant_id, Product.deleted_at.is_(None))
    if ally_id is not None:
        if ally_id == "none":
            filtro = filtro.where(Product.ally_id.is_(None))
        else:
            try:
                filtro = filtro.where(Product.ally_id == uuid.UUID(ally_id))
            except ValueError:
                raise ApiError.bad_request("ally_id inválido.", "invalid_ally_id") from None
    async with get_session() as session:
        products = (
            (await session.execute(filtro.order_by(Product.name)))
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
                ally_id=p.ally_id,
                price=float(product_variants[0].price or 0) if product_variants else 0.0,
                image_url=url_foto(p),
                image_url_externa=p.image_url,
                image_key=p.image_key,
                variants=[
                    VariantOut(
                        id=v.id,
                        sku=v.sku,
                        attributes=v.attributes or {},
                        price=float(v.price or 0),
                        stock=inv_by_variant.get(v.id, 0),
                        active=v.active,
                        billing_period=v.billing_period,
                        trial_days=v.trial_days,
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
    slug = await tenant_slug(tenant_id) if tenant_id is not None else None
    viejo_key: str | None = None
    async with get_session() as session:
        # Defensa en profundidad: no dejar la categoría colgando a otro tenant.
        resolved_category_id: uuid.UUID | None = None
        if payload.category_id is not None:
            category = await get_owned(session, Category, payload.category_id, tenant_id)
            if category is not None:
                resolved_category_id = category.id
        cambia_aliado = "ally_id" in payload.model_fields_set
        resolved_ally_id = await _resolver_aliado(session, tenant_id, payload.ally_id) if cambia_aliado else None

        if payload.product_id is not None:
            product = await get_owned(session, Product, payload.product_id, tenant_id)
            if product is None:
                raise ApiError.not_found("Producto no encontrado.")
            product.name = payload.name
            product.description = payload.description
            product.category_id = resolved_category_id
            if payload.image_url is not None:
                product.image_url = payload.image_url or None
            if payload.image_key is not None:
                nueva_key = payload.image_key or None
                if nueva_key is not None and not _key_producto_valida(nueva_key, slug):
                    raise ApiError.bad_request("La foto subida no es válida.", "image_key_invalida")
                if product.image_key and product.image_key != nueva_key:
                    viejo_key = product.image_key
                product.image_key = nueva_key
            if cambia_aliado:
                product.ally_id = resolved_ally_id
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
            await _validar_plan(tenant_id, payload, bool(variant and variant.billing_period))
            if variant is not None:
                if payload.sku:
                    variant.sku = payload.sku
                if payload.price:
                    variant.price = payload.price
                if payload.billing_period is not None:
                    variant.billing_period = payload.billing_period or None
                if payload.trial_days is not None:
                    variant.trial_days = payload.trial_days or None
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
            await _validar_plan(tenant_id, payload, ya_recurrente=False)
            nueva_key = payload.image_key or None
            if nueva_key is not None and not _key_producto_valida(nueva_key, slug):
                raise ApiError.bad_request("La foto subida no es válida.", "image_key_invalida")
            product = Product(
                name=payload.name,
                description=payload.description or "",
                category_id=resolved_category_id,
                image_url=payload.image_url or None,
                image_key=nueva_key,
                ally_id=resolved_ally_id,
            )
            session.add(product)
            await session.flush()
            variant = ProductVariant(
                product_id=product.id,
                sku=payload.sku or f"SKU-{product.id.hex[:8]}",
                price=payload.price,
                billing_period=payload.billing_period or None,
                trial_days=payload.trial_days or None,
            )
            session.add(variant)
            await session.flush()
            session.add(Inventory(variant_id=variant.id, stock_quantity=payload.stock))
            product.has_variants = False
    if viejo_key:
        # La foto anterior se reemplazó: se borra después de confirmar el cambio de clave.
        # Solo dentro de la carpeta de fotos del propio negocio: una key vieja sospechosa no borra nada.
        if slug:
            almacenamiento.borrar(viejo_key, dentro_de=almacenamiento.carpeta_de("producto", slug))
    return Response(status_code=204)


@router.post("/products/image", status_code=201)
async def upload_product_image(
    archivo: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> dict[str, str]:
    """Sube la foto de un producto al controlador único (HU-CRM-05) y devuelve su clave y URL.

    El panel/APK la suben antes de guardar el producto; luego mandan `image_key` en el alta."""
    tenant_id = principal.tenant_id
    slug = await tenant_slug(tenant_id) if tenant_id is not None else None
    if slug is None:
        raise ApiError.bad_request("No hay negocio activo.", "no_tenant")
    limite = almacenamiento.PERFILES["producto"].max_bytes
    contenido = await archivo.read(limite + 1)
    try:
        ref = almacenamiento.guardar(
            "producto", contenido, mimetype=archivo.content_type, tenant_slug=slug
        )
    except almacenamiento.StorageInvalido as exc:
        raise ApiError.bad_request(str(exc), exc.codigo) from exc
    return {"image_key": ref.key, "image_url": ref.url}


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
    sort_order: int = Field(default=0, ge=0, le=100_000)


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


# ------------------------------------------------------------------ aliados (HU-CRM-03)
class ModulosOut(BaseModel):
    """Módulos opcionales del pack encendidos en el negocio (el panel decide qué mostrar)."""

    aliados: bool


class AllyContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contacto: str | None = Field(default=None, max_length=120)
    telefono: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=254)
    notas: str | None = Field(default=None, max_length=500)


class AllyOut(BaseModel):
    id: uuid.UUID
    name: str
    contact_info: AllyContact
    active: bool
    products: int
    created_at: datetime | None = None


class AllySave(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    contact_info: AllyContact = Field(default_factory=AllyContact)
    active: bool = True


class AllyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    contact_info: AllyContact | None = None
    active: bool | None = None


class AliadosEstado(BaseModel):
    habilitado: bool
    por: str | None = None
    en: datetime | None = None


class AliadosCambio(BaseModel):
    habilitado: bool


async def _exigir_aliados(tenant_id: uuid.UUID | None) -> None:
    # Con el módulo apagado los aliados no existen para el negocio: 404, no 403.
    if not await aliados.habilitados(tenant_id):
        raise ApiError(404, "aliados_deshabilitados", "Los aliados no están habilitados en este negocio.")


async def _resolver_aliado(session: Any, tenant_id: uuid.UUID | None, ally_id: uuid.UUID | None) -> uuid.UUID | None:
    """El aliado a guardar en un producto o una promoción. `None` lo quita (siempre se puede,
    aun con el módulo apagado); asignar uno exige el módulo encendido y un aliado vivo del
    mismo negocio (si es de otro negocio, 404 como si no existiera)."""
    if ally_id is None:
        return None
    await _exigir_aliados(tenant_id)
    ally = await get_owned(session, Ally, ally_id, tenant_id)
    if ally is None or ally.deleted_at is not None:
        raise ApiError.not_found("Aliado no encontrado.")
    return ally.id


def _contacto(raw: Any) -> AllyContact:
    datos = raw if isinstance(raw, dict) else {}
    return AllyContact(**{k: datos.get(k) for k in AllyContact.model_fields if datos.get(k)})


def _contacto_guardado(contacto: AllyContact) -> dict[str, str]:
    return {k: v.strip() for k, v in contacto.model_dump().items() if isinstance(v, str) and v.strip()}


async def _nombre_libre(session: Any, tenant_id: uuid.UUID | None, nombre: str, excepto: uuid.UUID | None = None) -> None:
    q = select(Ally.id).where(
        Ally.tenant_id == tenant_id,
        Ally.deleted_at.is_(None),
        func.lower(Ally.name) == nombre.lower(),
    )
    if excepto is not None:
        q = q.where(Ally.id != excepto)
    if (await session.execute(q)).first() is not None:
        raise ApiError(409, "aliado_duplicado", f"Ya existe un aliado llamado «{nombre}».")


async def _ally_out(session: Any, tenant_id: uuid.UUID | None, ally: Ally) -> AllyOut:
    productos = (
        await session.execute(
            select(func.count())
            .select_from(Product)
            .where(
                Product.tenant_id == tenant_id,
                Product.ally_id == ally.id,
                Product.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    return AllyOut(
        id=ally.id,
        name=ally.name,
        contact_info=_contacto(ally.contact_info),
        active=bool(ally.active),
        products=int(productos or 0),
        created_at=ally.created_at,
    )


@router.get("/modulos", response_model=ModulosOut)
async def commerce_modulos(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
) -> ModulosOut:
    return ModulosOut(aliados=await aliados.habilitados(principal.tenant_id))


class MinimoPagoEnLineaOut(BaseModel):
    """Mínimo por cobro para pagar en línea (HU-SUB-25). El panel lo usa para avisar al guardar un plan."""

    minimo: float | None = Field(
        description="Monto mínimo por cobro en la moneda del negocio; `null` si no hay pasarela que lo declare.",
    )
    fuente: Literal["negocio", "pasarela", "ninguna"] = Field(
        description="`negocio`: lo fijó el negocio; `pasarela`: lo declara la pasarela para su ambiente.",
    )
    moneda: str = "COP"
    cuentas_de_transferencia: int = Field(
        description="Cuentas de transferencia configuradas (0 = el cobro bajo el mínimo no tendría cómo pagarse).",
    )


@router.get("/pago-en-linea/minimo", response_model=MinimoPagoEnLineaOut)
async def minimo_pago_en_linea(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
) -> MinimoPagoEnLineaOut:
    from .minimo import cuentas_de_transferencia_configuradas, minimo_efectivo
    from .skills import _plugin_config

    minimo, fuente = await minimo_efectivo(await _plugin_config())
    return MinimoPagoEnLineaOut(
        minimo=minimo, fuente=fuente, cuentas_de_transferencia=await cuentas_de_transferencia_configuradas()
    )


@router.get("/allies", response_model=list[AllyOut])
async def list_allies(
    principal: Annotated[Principal, Depends(require("pack_commerce"))],
) -> list[AllyOut]:
    tenant_id = principal.tenant_id
    await _exigir_aliados(tenant_id)
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Ally)
                .where(Ally.tenant_id == tenant_id, Ally.deleted_at.is_(None))
                .order_by(func.lower(Ally.name))
                .limit(500)
            )
        ).scalars().all()
        conteo = dict(
            (
                await session.execute(
                    select(Product.ally_id, func.count())
                    .where(
                        Product.tenant_id == tenant_id,
                        Product.ally_id.is_not(None),
                        Product.deleted_at.is_(None),
                    )
                    .group_by(Product.ally_id)
                )
            ).all()
        )
    return [
        AllyOut(
            id=a.id,
            name=a.name,
            contact_info=_contacto(a.contact_info),
            active=bool(a.active),
            products=int(conteo.get(a.id, 0)),
            created_at=a.created_at,
        )
        for a in rows
    ]


@router.post("/allies", response_model=AllyOut, status_code=201)
async def create_ally(
    payload: AllySave,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> AllyOut:
    tenant_id = principal.tenant_id
    await _exigir_aliados(tenant_id)
    nombre = payload.name.strip()
    if not nombre:
        raise ApiError.bad_request("El aliado necesita un nombre.", "invalid_name")
    async with get_session() as session:
        await _nombre_libre(session, tenant_id, nombre)
        ally = Ally(name=nombre, contact_info=_contacto_guardado(payload.contact_info), active=payload.active)
        session.add(ally)
        await session.flush()
        return await _ally_out(session, tenant_id, ally)


@router.patch("/allies/{ally_id}", response_model=AllyOut)
async def update_ally(
    ally_id: uuid.UUID,
    payload: AllyPatch,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> AllyOut:
    tenant_id = principal.tenant_id
    await _exigir_aliados(tenant_id)
    async with get_session() as session:
        ally = await get_owned(session, Ally, ally_id, tenant_id)
        if ally is None or ally.deleted_at is not None:
            raise ApiError.not_found("Aliado no encontrado.")
        if payload.name is not None:
            nombre = payload.name.strip()
            if not nombre:
                raise ApiError.bad_request("El aliado necesita un nombre.", "invalid_name")
            await _nombre_libre(session, tenant_id, nombre, excepto=ally.id)
            ally.name = nombre
        if payload.contact_info is not None:
            ally.contact_info = _contacto_guardado(payload.contact_info)
        if payload.active is not None:
            ally.active = payload.active
        await session.flush()
        return await _ally_out(session, tenant_id, ally)


@router.delete("/allies/{ally_id}", status_code=204)
async def delete_ally(
    ally_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
) -> Response:
    """Borrado lógico. Sus productos quedan «sin aliado» y sus promociones se desactivan
    (conservan el aliado): quitarles el aliado las abriría a todo el catálogo."""
    tenant_id = principal.tenant_id
    await _exigir_aliados(tenant_id)
    from datetime import UTC

    async with get_session() as session:
        ally = await get_owned(session, Ally, ally_id, tenant_id)
        if ally is None or ally.deleted_at is not None:
            raise ApiError.not_found("Aliado no encontrado.")
        ally.deleted_at = datetime.now(UTC)
        ally.active = False
        await session.execute(
            update(Product)
            .where(Product.tenant_id == tenant_id, Product.ally_id == ally.id)
            .values(ally_id=None)
        )
        await session.execute(
            update(Promotion)
            .where(Promotion.tenant_id == tenant_id, Promotion.ally_id == ally.id)
            .values(active=False)
        )
    return Response(status_code=204)


# Plataforma: solo el master enciende o apaga los aliados de un negocio (D-SPEC-33-2,
# Negocios › Acciones). Vive en el pack para que el core no conozca el dominio.
def _require_master(principal: Principal) -> None:
    from core.admin.roles import es_master

    if not es_master(principal.admin):
        raise ApiError.forbidden("Solo el admin master enciende los aliados de un negocio.")


def _estado_out(valor: dict[str, Any]) -> AliadosEstado:
    en = valor.get("en")
    return AliadosEstado(
        habilitado=bool(valor.get("habilitado")),
        por=valor.get("por"),
        en=datetime.fromisoformat(en) if isinstance(en, str) else None,
    )


@router.get("/platform/tenants/{tenant_id}/aliados", response_model=AliadosEstado)
async def get_aliados_del_negocio(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AliadosEstado:
    _require_master(principal)
    valor = await aliados.estado(tenant_id)
    if valor is None:
        raise ApiError.not_found("Negocio no encontrado.")
    return _estado_out(valor)


@router.put("/platform/tenants/{tenant_id}/aliados", response_model=AliadosEstado)
async def put_aliados_del_negocio(
    tenant_id: uuid.UUID,
    payload: AliadosCambio,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AliadosEstado:
    """Enciende o apaga los aliados. Apagar no borra nada: aliados y asignaciones se
    conservan para cuando se vuelva a encender."""
    _require_master(principal)
    try:
        valor = await aliados.fijar(tenant_id, habilitar=payload.habilitado, por=f"admin:{principal.admin.email}")
    except LookupError:
        raise ApiError.not_found("Negocio no encontrado.") from None
    return _estado_out(valor)


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
    ally_id: uuid.UUID | None = None  # HU-CRM-03
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
    # HU-CRM-03: si no viene, no cambia; `null` lo quita. Con aliado, la promoción aplica
    # solo si todos los productos del carrito son de ese aliado.
    ally_id: uuid.UUID | None = None
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
        ally_id=p.ally_id,
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
        cambia_aliado = "ally_id" in payload.model_fields_set
        resolved_ally = await _resolver_aliado(session, tenant_id, payload.ally_id) if cambia_aliado else None

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
            if cambia_aliado:
                promotion.ally_id = resolved_ally
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
                    ally_id=resolved_ally,
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
    # HU-SUB-09: activación del plan (None si el pedido no lleva planes).
    plan_activation_status: str | None = None
    plan_activation_error: str | None = None


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
                    ProductVariant.tenant_id == tenant_id,
                    Product.tenant_id == tenant_id,
                    Product.deleted_at.is_(None),
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
            plan_activation_status=o.plan_activation_status,
            plan_activation_error=o.plan_activation_error,
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
    # HU-CRM-03: nombres de la columna `aliado` que no corresponden a ningún aliado (la fila
    # se crea sin aliado). Vacío si los aliados están apagados.
    unknown_allies: list[str] = []


@router.post("/products/import", response_model=ImportResult, status_code=201)
async def import_products_csv(
    file: Annotated[UploadFile, File()],
    principal: Annotated[Principal, Depends(require("pack_commerce", write=True))],
    # HU-CRM-03: aliado de las filas sin columna `aliado` (o con ella vacía).
    ally_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ImportResult:
    """Importa productos desde un CSV (columnas `name` y `price`, paridad con el clásico).

    Opcionales: `description`, `sku`, `stock` e `image_url` (HU-CRM-01; una URL que no sea
    https se ignora y el producto se crea sin foto)."""
    import csv
    import io

    contenido = (await file.read()).decode("utf-8", errors="ignore")
    filas = list(csv.DictReader(io.StringIO(contenido)))
    if not filas or "name" not in filas[0] or "price" not in filas[0]:
        raise ApiError.bad_request("CSV inválido: requiere columnas name y price", "invalid_csv")

    creados = 0
    desconocidos: list[str] = []
    async with get_session() as session:
        aliado_por_defecto = await _resolver_aliado(session, principal.tenant_id, ally_id)
        aliado_por_nombre: dict[str, uuid.UUID] = {}
        con_aliados = "aliado" in filas[0] and await aliados.habilitados(principal.tenant_id)
        if con_aliados:
            aliado_por_nombre = {
                a.name.strip().lower(): a.id
                for a in (
                    await session.execute(
                        select(Ally).where(Ally.tenant_id == principal.tenant_id, Ally.deleted_at.is_(None))
                    )
                ).scalars()
            }
        for fila in filas:
            nombre = str(fila.get("name") or "").strip()
            if not nombre:
                continue
            try:
                precio = Decimal(str(fila.get("price") or "0").replace(",", "").strip())
            except Exception:
                continue
            try:
                foto = _image_url_valida(str(fila.get("image_url") or "")[:IMAGE_URL_MAX + 1])
            except ValueError:
                foto = None
            if foto and len(foto) > IMAGE_URL_MAX:
                foto = None
            aliado_fila = aliado_por_defecto
            nombre_aliado = str(fila.get("aliado") or "").strip()
            if con_aliados and nombre_aliado:
                aliado_fila = aliado_por_nombre.get(nombre_aliado.lower())
                if aliado_fila is None and nombre_aliado not in desconocidos:
                    desconocidos.append(nombre_aliado)
            producto = Product(
                name=nombre,
                description=str(fila.get("description") or "").strip(),
                category_id=None,
                image_url=foto or None,
                ally_id=aliado_fila,
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
    return ImportResult(created=creados, unknown_allies=desconocidos[:50])
