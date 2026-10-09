"""Metadatos de recurrencia del plan (ADR-0083, #153): seed + tool informar_plan."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CATALOG = {
    "pack": "pack_commerce",
    "category_paths": [["Planes"]],
    "products": [
        {
            "name": "Plan Basic",
            "description": "Plan recurrente de prueba.",
            "category_path": ["Planes"],
            "variants": [
                {
                    "sku": "PLAN-BASIC",
                    "attributes": {"periodicidad": "mensual", "trial_dias": 30, "limites": {"clientes": 500, "mensajes": 15000}},
                    "price": 200000,
                    "stock": 9999,
                }
            ],
        }
    ],
}


async def test_seed_mapea_metadatos_de_recurrencia() -> None:
    from pack_commerce.models import ProductVariant
    from sqlalchemy import select

    from cli.commands.tenant import seed_catalog
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("crmplan")
    tenant_id, _ = await onboard_tenant(
        name=f"Plan {slug}",
        slug=slug,
        business_description="Planes de prueba.",
        persona={"tone": "cercano"},
        packs=["pack_commerce"],
    )
    await seed_catalog(tenant_id, CATALOG)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            v = (
                await session.execute(
                    select(ProductVariant).where(
                        ProductVariant.tenant_id == tenant_id,
                        ProductVariant.sku == "PLAN-BASIC",
                    )
                )
            ).scalars().first()
            assert v is not None
            assert v.billing_period == "mensual"
            assert v.trial_days == 30
            assert (v.entitlements or {}).get("clientes") == 500


async def test_informar_plan_devuelve_metadatos() -> None:
    from pack_commerce.models import Product, ProductVariant
    from pack_commerce.skills import CatalogSkill

    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    slug = uniq("crmplan2")
    tenant_id, _ = await onboard_tenant(
        name=f"Plan {slug}",
        slug=slug,
        business_description="Planes de prueba.",
        persona={"tone": "cercano"},
        packs=["pack_commerce"],
    )
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            product = Product(tenant_id=tenant_id, name="Plan Basic", description="Recurrente", has_variants=False)
            session.add(product)
            await session.flush()
            session.add(
                ProductVariant(
                    tenant_id=tenant_id,
                    product_id=product.id,
                    sku="PLAN-BASIC",
                    price=200000,
                    billing_period="mensual",
                    trial_days=30,
                    entitlements={"clientes": 500},
                )
            )
            await session.flush()

    ctx = ToolContext(
        conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(),
        idempotency_key=str(uuid.uuid4()), channel="test",
    )
    skill = CatalogSkill()
    async with tenant_scope(tenant_id):
        plan = await skill.informar_plan({"sku": "PLAN-BASIC"}, ctx)
        assert plan["encontrado"] is True
        assert plan["billing_period"] == "mensual"
        assert plan["trial_days"] == 30
        assert plan["entitlements"]["clientes"] == 500
        assert plan["price"] == 200000

        # One-shot: sin recurrencia → aviso.
        one = await skill.informar_plan({"sku": "NO-EXISTE"}, ctx)
        assert one["encontrado"] is False