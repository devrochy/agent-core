"""HU-CRM-01: `consultar_inventario` manda la foto solo con un único resultado puntual.

Contra la base real: el SKU de otro negocio no trae su foto (aislamiento), una lista de
varios no la lleva y un único resultado por SKU sí. La regla en detalle está en
`tests/test_crm01_foto_producto.py`.
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID, TEST_BETA_ID

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID
T2 = TEST_BETA_ID


async def _producto(tenant_id, *, name: str, sku: str, image_url: str | None) -> None:
    from pack_commerce.models import Inventory, Product, ProductVariant

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            product = Product(tenant_id=tenant_id, name=name, image_url=image_url)
            session.add(product)
            await session.flush()
            variant = ProductVariant(tenant_id=tenant_id, product_id=product.id, sku=sku, price=50_000)
            session.add(variant)
            await session.flush()
            session.add(Inventory(tenant_id=tenant_id, variant_id=variant.id, stock_quantity=3))


async def _consultar(tenant_id, arguments: dict) -> dict:
    from pack_commerce.skills import CatalogSkill

    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    ctx = ToolContext(
        conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), idempotency_key="t", channel="whatsapp"
    )
    async with tenant_scope(tenant_id):
        return await CatalogSkill().consultar_inventario(arguments, ctx)


async def test_foto_solo_con_unico_resultado_y_del_propio_negocio():
    s = uuid.uuid4().hex[:6]
    foto = f"https://cdn.ejemplo.com/{s}.jpg"
    await _producto(T1, name=f"Casco Rojo {s}", sku=f"CR-{s}", image_url=foto)
    await _producto(T1, name=f"Casco Azul {s}", sku=f"CA-{s}", image_url=foto)
    await _producto(T1, name=f"Guantes {s}", sku=f"GU-{s}", image_url=None)
    await _producto(T1, name=f"Botas {s}", sku=f"VI-{s}", image_url=f"http://viejo.com/{s}.jpg")
    await _producto(T2, name=f"Casco Ajeno {s}", sku=f"CX-{s}", image_url=f"https://otro.com/{s}.jpg")

    por_sku = await _consultar(T1, {"sku": f"CR-{s}"})
    assert por_sku["_image_url"] == foto
    assert por_sku["matches"][0]["tiene_foto"] is True

    lista = await _consultar(T1, {"query": f"casco {s}"})
    assert len(lista["matches"]) >= 2  # la búsqueda por tokens trae varios
    assert "_image_url" not in lista

    sin_foto = await _consultar(T1, {"sku": f"GU-{s}"})
    assert len(sin_foto["matches"]) == 1 and "_image_url" not in sin_foto
    assert sin_foto["matches"][0]["tiene_foto"] is False

    vieja = await _consultar(T1, {"sku": f"VI-{s}"})
    assert "_image_url" not in vieja and vieja["matches"][0]["tiene_foto"] is False

    ajeno = await _consultar(T1, {"sku": f"CX-{s}"})
    assert ajeno["matches"] == [] and "_image_url" not in ajeno
