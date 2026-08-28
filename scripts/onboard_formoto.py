"""Onboarding de FORMOTO (tenant #2) sobre pack-commerce — Sprint 4, Fase C.

Demuestra el hito del sprint: dar de alta un segundo cliente de comercio es
100% configuración (datos + config, cero código por cliente). Mide el tiempo.

Catálogo portado de agent-sale (`scripts/seed-catalogo-prueba.ts`): 100 productos
de repuestos/accesorios de moto, precios COP, 17 paths de categoría y 2 pares de
complementos. La compatibilidad técnica ("¿le sirve a mi moto?") se resuelve como
escalación/configuración (triggers de escalación + copy), NO como motor de fitment:
el negocio real (agent-sale) la trata como motivo de escalación a humano.

Uso:  .venv/bin/python -m scripts.onboard_formoto
Idempotente: se puede correr varias veces sin duplicar.
"""

import asyncio
import time
import uuid
from copy import deepcopy

from sqlalchemy import select

from core.agents.spec import DEFAULT_SPEC
from core.db.models import (
    Admin,
    AgentSpecRecord,
    AppSettings,
    ChannelConnection,
    Tenant,
    TenantUser,
)
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from core.shared.tenant import tenant_scope
from scripts.formoto_catalog import (
    FORMOTO_CATEGORY_PATHS,
    FORMOTO_COMPLEMENT_PAIRS,
    FORMOTO_PRODUCTS,
)

FORMOTO_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
FORMOTO_SLUG = "formoto"
FORMOTO_NAME = "FORMOTO — Repuestos y accesorios de moto"
FORMOTO_PHONE = "9876543210"  # dev: número WhatsApp fake (resuelve el tenant en el webhook)
ADMIN_EMAIL = "admin@local"

# Enablement por tenant (ADR-0019). Sin pack de fitment: la compatibilidad técnica
# es escalación/configuración (C2, validado contra el negocio real en agent-sale).
FORMOTO_PACKS = ["pack_commerce"]

# Extensión del AgentSpec default: persona de motos + trigger de escalación por
# compatibilidad técnica (patrón de agent-sale: escalar a humano si no se resuelve).
FORMOTO_PERSONA = {
    "role": "asistente de ventas de ForMotos, tienda de repuestos y accesorios de moto",
    "tone": "cercano",
    "warmth": 3,
    "emojis": "sparse",
    "maxLength": 500,
    "forbiddenPhrases": ["claro, tenemos motos"],
    "locale": "es-CO",
}
FORMOTO_ESCALATION_TRIGGERS = ["asesor", "humano", "persona real", "reclamo", "queja", "compatibilidad"]
FORMOTO_ESCALATION_CONFIG = {
    "keywords": ["compatibilidad", "asesor", "reclamo", "humano"],
    "max_turnos_sin_resolver": 3,
}


def _formoto_spec_json() -> dict:
    spec = deepcopy(DEFAULT_SPEC)
    spec["tenantId"] = str(FORMOTO_ID)
    spec["persona"] = FORMOTO_PERSONA
    spec["packs"] = list(FORMOTO_PACKS)
    spec["policies"]["escalation"]["triggers"] = list(FORMOTO_ESCALATION_TRIGGERS)
    return spec


async def _upsert_catalog() -> None:
    """Crea el árbol de categorías, los 100 productos con variantes/inventario y
    los complementos. Idempotente: si el SKU ya existe, actualiza; si no, inserta."""
    from pack_commerce.models import (
        Category,
        CategoryComplement,
        Inventory,
        Product,
        ProductVariant,
    )

    category_ids: dict[tuple[str, ...], uuid.UUID] = {}
    _order = 0

    async def category_id(path: tuple[str, ...]) -> uuid.UUID:
        nonlocal _order
        if path in category_ids:
            return category_ids[path]
        parent = await category_id(path[:-1]) if len(path) > 1 else None
        stmt = select(Category).where(
            Category.tenant_id == FORMOTO_ID,
            Category.name == path[-1],
            Category.parent_id.is_(None) if parent is None else Category.parent_id == parent,
        )
        cat = (await session.execute(stmt)).scalars().first()
        if cat is None:
            cat = Category(parent_id=parent, name=path[-1], sort_order=_order, active=True)
            session.add(cat)
            await session.flush()
            _order += 1
        category_ids[path] = cat.id
        return cat.id

    async with tenant_scope(FORMOTO_ID):
        async with get_session() as session:
            for path in FORMOTO_CATEGORY_PATHS:
                await category_id(tuple(path))
            await session.flush()

            for producto in FORMOTO_PRODUCTS:
                cat = await category_id(tuple(producto["category_path"]))
                has_variants = len(producto["variants"]) > 1
                base_sku = producto["variants"][0]["sku"]
                # lookups SIEMPRE acotados al tenant (defensa más allá de RLS):
                # un SKU de otro tenant no puede secuestrar el producto.
                prod_stmt = (
                    select(Product)
                    .join(ProductVariant, ProductVariant.product_id == Product.id)
                    .where(
                        ProductVariant.sku == base_sku,
                        Product.tenant_id == FORMOTO_ID,
                    )
                )
                product = (await session.execute(prod_stmt)).scalars().first()
                if product is None:
                    product = Product(
                        name=producto["name"],
                        description=producto["description"],
                        image_url=producto["image_url"],
                        category_id=cat,
                        has_variants=has_variants,
                    )
                    session.add(product)
                    await session.flush()

                for variante in producto["variants"]:
                    vstmt = select(ProductVariant).where(
                        ProductVariant.sku == variante["sku"],
                        ProductVariant.tenant_id == FORMOTO_ID,
                    )
                    variant = (await session.execute(vstmt)).scalars().first()
                    if variant is None:
                        variant = ProductVariant(
                            product_id=product.id,
                            sku=variante["sku"],
                            attributes=variante["attributes"],
                            price=variante["price"],
                            active=True,
                        )
                        session.add(variant)
                        await session.flush()
                    # inventario por variante: inserta SOLO si falta (idempotente,
                    # sin borrado físico — invariante #8). El stock de un re-seed
                    # no se sobreescribe; para refrescar stock hay que borrar la
                    # fila por el panel/import.
                    existing_stock = (
                        await session.execute(
                            select(Inventory).where(
                                Inventory.variant_id == variant.id,
                                Inventory.tenant_id == FORMOTO_ID,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_stock is None:
                        session.add(
                            Inventory(
                                variant_id=variant.id,
                                stock_quantity=variante["stock"],
                                source="import",
                            )
                        )
                await session.flush()

            # complementos (ambas direcciones)
            for path_a, path_b in FORMOTO_COMPLEMENT_PAIRS:
                id_a = category_ids.get(tuple(path_a))
                id_b = category_ids.get(tuple(path_b))
                if id_a is None or id_b is None:
                    continue
                for (a, b) in ((id_a, id_b), (id_b, id_a)):
                    exists = (
                        await session.execute(
                            select(CategoryComplement).where(
                                CategoryComplement.tenant_id == FORMOTO_ID,
                                CategoryComplement.category_id == a,
                                CategoryComplement.complementary_category_id == b,
                            )
                        )
                    ).scalar_one_or_none()
                    if exists is None:
                        session.add(
                            CategoryComplement(category_id=a, complementary_category_id=b)
                        )
            await session.commit()


async def onboard() -> list[str]:
    """Onboarding idempotente de FORMOTO. Devuelve los pasos ejecutados."""
    plugin_manager.scan()
    results: list[str] = []

    async with get_session() as session:
        # 1. tenant (tabla de sistema, sin RLS)
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == FORMOTO_SLUG))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(id=FORMOTO_ID, name=FORMOTO_NAME, slug=FORMOTO_SLUG, status="active")
            session.add(tenant)
            await session.flush()
            results.append("tenant creado")
        else:
            results.append("tenant ya existía")

        # admin master (tabla de sistema, sin RLS)
        admin = (
            await session.execute(select(Admin).where(Admin.email == ADMIN_EMAIL))
        ).scalar_one_or_none()

    # 2-5. filas de negocio (RLS): el scope DEBE rodear la sesión, porque
    # get_session() lee el tenant_scope al abrir (ADR-0012).
    async with tenant_scope(FORMOTO_ID):
        async with get_session() as session:
            app_settings = (
                await session.execute(
                    select(AppSettings).where(AppSettings.tenant_id == FORMOTO_ID)
                )
            ).scalar_one_or_none()
            if app_settings is None:
                session.add(
                    AppSettings(
                        tenant_id=FORMOTO_ID,
                        assistant_name="ForMotos",
                        business_description=(
                            "Tienda de repuestos y accesorios de moto: cascos, llantas, "
                            "bujías, baterías, aceites, cadenas y más. Ventas por WhatsApp."
                        ),
                        language="es",
                        escalation_config=FORMOTO_ESCALATION_CONFIG,
                    )
                )
                results.append("app_settings creados")
            else:
                results.append("app_settings ya existían")

            spec_row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == FORMOTO_ID,
                        AgentSpecRecord.is_active.is_(True),
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if spec_row is None:
                session.add(
                    AgentSpecRecord(
                        tenant_id=FORMOTO_ID,
                        version="1.0.0",
                        is_active=True,
                        spec=_formoto_spec_json(),
                    )
                )
                results.append(f"agent_spec creado (packs={','.join(FORMOTO_PACKS)})")
            else:
                results.append("agent_spec ya existía")

            if admin is not None:
                link = (
                    await session.execute(
                        select(TenantUser).where(
                            TenantUser.tenant_id == FORMOTO_ID, TenantUser.admin_id == admin.id
                        )
                    )
                ).scalar_one_or_none()
                if link is None:
                    session.add(
                        TenantUser(tenant_id=FORMOTO_ID, admin_id=admin.id, role="owner")
                    )
                    results.append("tenant_user (admin → formoto) creado")
                else:
                    results.append("tenant_user ya existía")
            else:
                results.append("⚠ admin@local no existe — salta el tenant_user")

            conn = (
                await session.execute(
                    select(ChannelConnection).where(
                        ChannelConnection.tenant_id == FORMOTO_ID,
                        ChannelConnection.external_id == FORMOTO_PHONE,
                    )
                )
            ).scalar_one_or_none()
            if conn is None:
                session.add(
                    ChannelConnection(
                        tenant_id=FORMOTO_ID,
                        channel="whatsapp",
                        provider="meta",
                        label="ForMotos WhatsApp",
                        external_id=FORMOTO_PHONE,
                        active=True,
                        is_primary=True,
                    )
                )
                results.append("conexión WhatsApp creada")
            else:
                results.append("conexión WhatsApp ya existía")
        await session.commit()

    # 6. config del pack por tenant + activación
    async with tenant_scope(FORMOTO_ID):
        await plugin_manager.save_config("pack_commerce", {})
        await plugin_manager.activate("pack_commerce")
    results.append("pack_commerce configurado y activo")

    # 7. catálogo (100 productos, COP)
    await _upsert_catalog()
    results.append("catálogo sembrado (100 productos)")
    return results


async def main() -> None:
    start = time.perf_counter()
    results = await onboard()
    elapsed = time.perf_counter() - start
    print(f"✓ Onboarding FORMOTO completado en {elapsed:.2f} s")
    for r in results:
        print("  ·", r)
    print("  · id tenant:", FORMOTO_ID)
    print("  · phone (webhook):", FORMOTO_PHONE)


if __name__ == "__main__":
    asyncio.run(main())