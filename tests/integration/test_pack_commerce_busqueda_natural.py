"""Búsqueda natural en `consultar_inventario` (pack-commerce).

Regresión real: el asistente buscó `{"query":"plan basic"}` y el catálogo tenía
el producto `Basic`; la búsqueda era `name ILIKE %query%` (frase completa) y no
matcheaba. Ahora se tokeniza la frase, se busca también en la descripción y, si
no hay match, se devuelven recomendaciones (top de productos disponibles).
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


async def _make_product(session, *, name: str, sku: str, price: float = 200_000.0, stock: int = 5, description: str = ""):
    from pack_commerce.models import Inventory, Product, ProductVariant

    product = Product(tenant_id=T1, name=name, description=description)
    session.add(product)
    await session.flush()
    variant = ProductVariant(tenant_id=T1, product_id=product.id, sku=sku, price=price)
    session.add(variant)
    await session.flush()
    session.add(Inventory(tenant_id=T1, variant_id=variant.id, stock_quantity=stock))
    await session.flush()
    return product.id


async def _cleanup(product_ids: list[uuid.UUID]) -> None:
    from pack_commerce.models import Inventory, Product, ProductVariant
    from sqlalchemy import delete

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            for pid in product_ids:
                variant_ids = (
                    await session.execute(
                        ProductVariant.__table__.select().where(ProductVariant.product_id == pid)
                    )
                ).all()
                for row in variant_ids:
                    await session.execute(delete(Inventory).where(Inventory.variant_id == row.id))
                await session.execute(delete(ProductVariant).where(ProductVariant.product_id == pid))
                await session.execute(delete(Product).where(Product.id == pid))


async def _search(query: str) -> dict:
    from pack_commerce.skills import CatalogSkill

    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    ctx = ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key="test",
        channel="whatsapp",
    )
    async with tenant_scope(T1):
        return await CatalogSkill().consultar_inventario({"query": query}, ctx)


async def test_query_frase_encuentra_producto_por_token():
    """'plan basic' debe encontrar el producto 'Basic' (match por token)."""
    suffix = uuid.uuid4().hex[:6]
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            pid = await _make_product(session, name=f"Basic {suffix}", sku=f"B-{suffix}")
    try:
        result = await _search(f"plan basic {suffix}")
        names = [m["name"] for m in result.get("matches", [])]
        assert f"Basic {suffix}" in names, result
    finally:
        await _cleanup([pid])


async def test_query_encuentra_por_descripcion():
    suffix = uuid.uuid4().hex[:6]
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            pid = await _make_product(
                session,
                name=f"Demo {suffix}",
                sku=f"D-{suffix}",
                description="Plan premium para empresas",
            )
    try:
        result = await _search(f"plan premium {suffix}")
        names = [m["name"] for m in result.get("matches", [])]
        assert f"Demo {suffix}" in names, result
    finally:
        await _cleanup([pid])


async def test_sin_match_devuelve_recomendaciones():
    suffix = uuid.uuid4().hex[:6]
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            pid = await _make_product(session, name=f"Plan Base {suffix}", sku=f"P-{suffix}")
    try:
        # Query con tokens que no aparecen en ningún producto (dos nonces distintos
        # del sufijo del nombre, para no matchear por token).
        nonce = f"{uuid.uuid4().hex[:8]} {uuid.uuid4().hex[:8]}"
        result = await _search(nonce)
        assert result.get("matches") == []
        assert result.get("message")
        assert result.get("recommendations"), result
    finally:
        await _cleanup([pid])
