"""Promociones en la API (`/api/v1/pack/commerce/promotions`).

Regresión: el guardado no exponía ni persistía categoría/producto/segmentos
(el formulario nuevo los perdía al editar) y no validaba nada: una promo de
volumen sin escalones o con descuento fuera de rango quedaba guardada y el
motor la ignoraba en silencio.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _tenant_con_catalogo() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    from pack_commerce.models import Category, Product

    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("promos-api")
    tid, _ = await onboard_tenant(
        name=f"Tenant promociones {slug}",
        slug=slug,
        business_description="Test de promociones.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            categoria = Category(tenant_id=tid, name=f"Categoría {slug}")
            session.add(categoria)
            await session.flush()
            producto = Product(tenant_id=tid, name=f"Producto {slug}", category_id=categoria.id)
            session.add(producto)
            await session.flush()
            category_id, product_id = categoria.id, producto.id
    return tid, category_id, product_id


def _payload_completo(category_id: uuid.UUID, product_id: uuid.UUID) -> dict:
    return {
        "type": "campaña",
        "rules": {"label": "10% fieles", "discount_pct": 10, "once_per_customer": True},
        "valid_from": "2026-10-01T08:00:00Z",
        "valid_to": "2026-10-31T20:00:00Z",
        "category_id": str(category_id),
        "product_id": str(product_id),
        "include_child_categories": False,
        "eligible_segments": ["fiel", "nuevo"],
        "active": False,
    }


async def test_roundtrip_guarda_todos_los_campos(app_client) -> None:
    tid, category_id, product_id = await _tenant_con_catalogo()
    headers = await api_headers(app_client, tid)

    r = await app_client.post(
        "/api/v1/pack/commerce/promotions",
        headers=headers,
        json=_payload_completo(category_id, product_id),
    )
    assert r.status_code == 204, r.text

    r2 = await app_client.get("/api/v1/pack/commerce/promotions", headers=headers)
    assert r2.status_code == 200, r2.text
    promo = r2.json()[0]
    assert promo["type"] == "campaña"
    assert promo["rules"]["label"] == "10% fieles"
    assert promo["rules"]["once_per_customer"] is True
    assert promo["category_id"] == str(category_id)
    assert promo["product_id"] == str(product_id)
    assert promo["include_child_categories"] is False
    assert promo["eligible_segments"] == ["fiel", "nuevo"]
    assert promo["active"] is False
    assert promo["valid_from"].startswith("2026-10-01")
    assert promo["valid_to"].startswith("2026-10-31")
    assert promo["created_at"]


async def test_editar_preserva_alcance_y_segmentos(app_client) -> None:
    """Bug real: al editar, el panel no enviaba categoría/producto/segmentos y el
    backend los pisaba con NULL (la promo perdía su alcance)."""
    tid, category_id, product_id = await _tenant_con_catalogo()
    headers = await api_headers(app_client, tid)

    await app_client.post(
        "/api/v1/pack/commerce/promotions",
        headers=headers,
        json=_payload_completo(category_id, product_id),
    )
    promo = (await app_client.get("/api/v1/pack/commerce/promotions", headers=headers)).json()[0]

    editado = _payload_completo(category_id, product_id)
    editado["promotion_id"] = promo["id"]
    editado["rules"] = {"label": "15% fieles", "discount_pct": 15, "once_per_customer": True}
    r = await app_client.post("/api/v1/pack/commerce/promotions", headers=headers, json=editado)
    assert r.status_code == 204, r.text

    actualizada = (await app_client.get("/api/v1/pack/commerce/promotions", headers=headers)).json()[0]
    assert actualizada["rules"]["discount_pct"] == 15
    assert actualizada["category_id"] == str(category_id)
    assert actualizada["product_id"] == str(product_id)
    assert actualizada["eligible_segments"] == ["fiel", "nuevo"]


async def test_volumen_con_escalones(app_client) -> None:
    tid, _category_id, _product_id = await _tenant_con_catalogo()
    headers = await api_headers(app_client, tid)

    payload = {
        "type": "volumen",
        "rules": {
            "label": "Por volumen",
            "tiers": [
                {"min": 3, "max": 5, "discount_pct": 10},
                {"min": 6, "discount_pct": 15},
                {"min": 10, "free_item_sku": "REGALO-1"},
            ],
        },
    }
    r = await app_client.post("/api/v1/pack/commerce/promotions", headers=headers, json=payload)
    assert r.status_code == 204, r.text
    promo = (await app_client.get("/api/v1/pack/commerce/promotions", headers=headers)).json()[0]
    assert len(promo["rules"]["tiers"]) == 3


@pytest.mark.parametrize(
    "parche",
    [
        {"type": "regalito"},  # tipo inválido
        {"rules": {"discount_pct": 150}},  # descuento fuera de rango
        {"valid_from": "2026-11-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},  # fechas
        {"rules": {"tiers": []}},  # volumen sin escalones
        {"rules": {"tiers": [{"min": 0, "discount_pct": 5}]}},  # mínimo inválido
        {"rules": {"tiers": [{"min": 3}]}},  # escalón sin beneficio
        {"eligible_segments": ["vip"]},  # segmento desconocido
    ],
)
async def test_validaciones_rechazan_promos_rotas(app_client, parche) -> None:
    tid, _category_id, _product_id = await _tenant_con_catalogo()
    headers = await api_headers(app_client, tid)

    payload = {"type": "temporada", "rules": {"discount_pct": 10}}
    payload.update(parche)
    r = await app_client.post("/api/v1/pack/commerce/promotions", headers=headers, json=payload)
    assert r.status_code == 400, f"debía rechazar {parche}: {r.text}"

    listado = (await app_client.get("/api/v1/pack/commerce/promotions", headers=headers)).json()
    assert listado == []
