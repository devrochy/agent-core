"""Siembra del catálogo de pack-commerce (ADR-0083, ADR-0110).

Antes vivía en el CLI (`cli/commands/tenant.py::seed_catalog`), que importaba los
modelos del pack. Ahora la expone el propio pack por `seed_data`: la usan el CLI
(`tenant seed`, `catalog.json`) y los presets (fragmento `seed`).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.shared.tenant import tenant_scope
from pack_commerce.models import (
    Category,
    CategoryComplement,
    Inventory,
    Product,
    ProductVariant,
)
from pack_commerce.precios_de_plan import ATRIBUTO, precio_de_plan


async def seed_catalog(
    tenant_id: uuid.UUID, data: dict[str, Any], creados: list[tuple[str, uuid.UUID]] | None = None
) -> list[str]:
    """Importa un catálogo pack_commerce a la BD del tenant. Idempotente.

    Los productos del tenant viven en `TENANTS_ROOT/<slug>/catalog.json` (dato,
    no código). Formato: {"category_paths": [[...]], "products": [...],
    "complement_pairs": [[...],[...]]}. Las filas se miran SIEMPRE acotadas al
    tenant (defensa más allá de RLS, ADR-0018).

    `creados` (datos de prueba, HU-QA-10): si viene, se le agrega `(kind, id)` de cada
    categoría, producto y variante que se CREA (no de lo que ya existía), para poder retirarlo.
    """
    steps: list[str] = []
    category_ids: dict[tuple[str, ...], uuid.UUID] = {}
    order = 0

    async def category_id(path: tuple[str, ...]) -> uuid.UUID:
        nonlocal order
        if path in category_ids:
            return category_ids[path]
        parent = await category_id(path[:-1]) if len(path) > 1 else None
        stmt = select(Category).where(
            Category.tenant_id == tenant_id,
            Category.name == path[-1],
            Category.deleted_at.is_(None),
            Category.parent_id.is_(None) if parent is None else Category.parent_id == parent,
        )
        cat = (await session.execute(stmt)).scalars().first()
        if cat is None:
            cat = Category(parent_id=parent, name=path[-1], sort_order=order, active=True)
            session.add(cat)
            await session.flush()
            order += 1
            if creados is not None:
                creados.append(("categoria", cat.id))
        category_ids[path] = cat.id
        return cat.id

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            for path in data.get("category_paths", []):
                await category_id(tuple(path))
            await session.flush()

            for producto in data.get("products", []):
                cat = await category_id(tuple(producto["category_path"]))
                has_variants = len(producto.get("variants", [])) > 1
                base_sku = producto["variants"][0]["sku"]
                prod_stmt = (
                    select(Product)
                    .join(ProductVariant, ProductVariant.product_id == Product.id)
                    .where(ProductVariant.sku == base_sku, Product.tenant_id == tenant_id)
                )
                product = (await session.execute(prod_stmt)).scalars().first()
                if product is None:
                    product = Product(
                        name=producto["name"],
                        description=producto.get("description", ""),
                        image_url=producto.get("image_url", ""),
                        category_id=cat,
                        has_variants=has_variants,
                    )
                    session.add(product)
                    await session.flush()
                    if creados is not None:
                        creados.append(("producto", product.id))

                for variante in producto.get("variants", []):
                    vstmt = select(ProductVariant).where(
                        ProductVariant.sku == variante["sku"],
                        ProductVariant.tenant_id == tenant_id,
                        ProductVariant.product_id == product.id,
                    )
                    variant = (await session.execute(vstmt)).scalars().first()
                    attrs = variante.get("attributes", {}) or {}
                    plan_meta = {
                        "billing_period": attrs.get("periodicidad") or attrs.get("billing_period"),
                        # `0` es un valor válido (plan pago sin prueba, D-16): no usar `or`.
                        "trial_days": next(
                            (attrs[k] for k in ("trial_dias", "trial_days") if attrs.get(k) is not None),
                            None,
                        ),
                        "entitlements": attrs.get("limites") or attrs.get("entitlements"),
                    }
                    # HU-SH-01: un plan de la plataforma toma su precio de core/billing.
                    precio = precio_de_plan(attrs)
                    if precio is None:
                        precio = variante["price"]
                    if variant is None:
                        variant = ProductVariant(
                            product_id=product.id,
                            sku=variante["sku"],
                            attributes=attrs,
                            price=precio,
                            active=True,
                            billing_period=plan_meta["billing_period"],
                            trial_days=int(plan_meta["trial_days"]) if plan_meta["trial_days"] is not None else None,
                            entitlements=plan_meta["entitlements"],
                        )
                        session.add(variant)
                        await session.flush()
                        if creados is not None:
                            creados.append(("variante", variant.id))
                    else:
                        variant.attributes = attrs
                        if attrs.get(ATRIBUTO):
                            variant.price = precio
                        if plan_meta["billing_period"]:
                            variant.billing_period = plan_meta["billing_period"]
                        if plan_meta["trial_days"] is not None:
                            variant.trial_days = int(plan_meta["trial_days"])
                        if plan_meta["entitlements"]:
                            variant.entitlements = plan_meta["entitlements"]
                    existing_stock = (
                        await session.execute(
                            select(Inventory).where(
                                Inventory.variant_id == variant.id,
                                Inventory.tenant_id == tenant_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_stock is None:
                        session.add(
                            Inventory(
                                variant_id=variant.id,
                                stock_quantity=variante.get("stock", 0),
                                source="import",
                            )
                        )
                await session.flush()

            for path_a, path_b in data.get("complement_pairs", []):
                id_a = category_ids.get(tuple(path_a))
                id_b = category_ids.get(tuple(path_b))
                if id_a is None or id_b is None:
                    continue
                for (a, b) in ((id_a, id_b), (id_b, id_a)):
                    exists = (
                        await session.execute(
                            select(CategoryComplement).where(
                                CategoryComplement.tenant_id == tenant_id,
                                CategoryComplement.category_id == a,
                                CategoryComplement.complementary_category_id == b,
                            )
                        )
                    ).scalar_one_or_none()
                    if exists is None:
                        session.add(CategoryComplement(category_id=a, complementary_category_id=b))
            await session.commit()
    steps.append(f"catálogo sembrado ({len(data.get('products', []))} productos)")
    return steps
