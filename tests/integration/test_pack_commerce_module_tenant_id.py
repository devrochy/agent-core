"""Defensa en profundidad en el panel admin (`pack_commerce/module.py`,
PR 2/2 de la fila 127 del ROADMAP -- `skills.py`+`jobs.py` ya se cubrieron
en el PR anterior). A diferencia de ese PR, acá el riesgo real es de
ESCRITURA: `promotion_toggle`/`order_update_status`/`category_save` no
verificaban `tenant_id` antes de mutar la fila -- si un admin con acceso a
dos tenants (switch-tenant, cookie `ac_tenant`) pasara el UUID de una fila
de OTRO tenant, sin el filtro explícito quedaba a merced de que RLS sola
no fallara nunca.

Mismo criterio que `test_pack_commerce_tenant_id_explicito.py`: un test
por FLUJO de escritura, no uno por cada uno de los ~31 call-sites.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _attacker_tenant():
    """Tenant "atacante" nuevo, con admin@local ya vinculado (`admin_tenants`
    exige una fila real en `tenant_user`; `TEST_ALPHA_ID`/`test-alpha` NO
    tiene ese vínculo -- solo se usa para queries directas por `tenant_scope`
    en otros tests, nunca a través del panel HTTP)."""
    from core.onboarding import onboard_tenant

    slug = f"attacker-{uuid.uuid4().hex[:8]}"
    tid, _steps = await onboard_tenant(name="Attacker", slug=slug, admin_email="admin@local")
    return tid


async def _login_and_switch(app_client, tenant_id) -> dict[str, str]:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    session = r.headers["set-cookie"].split(";")[0]
    r2 = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tenant_id)}
    )
    ac_tenant = next(c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c)
    return {"Cookie": f"{session}; {ac_tenant}"}


async def test_promotion_toggle_no_afecta_promocion_de_otro_tenant(app_client, moto_setup):
    """Admin activo en un tenant "atacante" intenta togglear el UUID de una
    promoción de `tenant-moto` -- no debe cambiar (antes del fix,
    `session.get(Promotion, promotion_id)` no filtraba tenant en
    absoluto)."""
    from pack_commerce.models import Promotion
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup

    async with tenant_scope(moto):
        async with get_session() as session:
            promo = Promotion(tenant_id=moto, type="temporada", rules={}, active=True)
            session.add(promo)
            await session.flush()
            promo_id = promo.id

    attacker = await _attacker_tenant()
    headers = await _login_and_switch(app_client, attacker)
    resp = await app_client.post(f"/crm/promociones/{promo_id}/toggle", headers=headers)
    assert resp.status_code == 303

    async with tenant_scope(moto):
        async with get_session() as session:
            fresh = (
                await session.execute(select(Promotion).where(Promotion.id == promo_id))
            ).scalar_one()
            assert fresh.active is True, "el toggle desde T1 no debe tocar una promoción de moto"

            await session.execute(Promotion.__table__.delete().where(Promotion.id == promo_id))


async def test_order_update_status_no_afecta_pedido_de_otro_tenant(app_client, moto_setup):
    """Mismo caso para `order_update_status` -- una escritura de estado
    de pedido no debe cruzar tenant."""
    from pack_commerce.models import Order
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup
    order_seq = 700_000 + uuid.uuid4().int % 90_000

    async with tenant_scope(moto):
        async with get_session() as session:
            order = Order(
                tenant_id=moto,
                conversation_id=uuid.uuid4(),
                customer_id=uuid.uuid4(),
                status="abierto",
                payment_method="efectivo_contraentrega",
                payment_status="pendiente",
                delivery_method="domicilio",
                total=50_000,
                order_seq=order_seq,
                public_order_number=f"OC-{order_seq:04d}",
                idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
            )
            session.add(order)
            await session.flush()
            order_id = order.id

    attacker = await _attacker_tenant()
    headers = await _login_and_switch(app_client, attacker)
    resp = await app_client.post(
        f"/crm/pedidos/{order_id}/estado", headers=headers, data={"status": "entregado"}
    )
    assert resp.status_code == 303

    async with tenant_scope(moto):
        async with get_session() as session:
            fresh = (
                await session.execute(select(Order).where(Order.id == order_id))
            ).scalar_one()
            assert fresh.status == "abierto", (
                "cambiar el estado desde otro tenant no debe tocar un pedido de moto"
            )

            await session.execute(Order.__table__.delete().where(Order.id == order_id))


async def test_category_save_no_reparenta_bajo_categoria_de_otro_tenant(app_client, moto_setup):
    """`category_save` valida que el `parent_id` exista y sea del tenant
    activo antes de asignarlo -- una categoría con `parent_id` apuntando al
    UUID de una categoría de `tenant-moto`, creada desde un tenant
    "atacante", debe rechazarse (antes del fix, `session.get(Category,
    parent)` no distinguía tenant)."""

    from pack_commerce.models import Category
    from sqlalchemy import func, select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup

    async with tenant_scope(moto):
        async with get_session() as session:
            parent = Category(tenant_id=moto, name=f"Categoría moto {uuid.uuid4().hex[:6]}")
            session.add(parent)
            await session.flush()
            parent_id = parent.id

    attacker = await _attacker_tenant()
    headers = await _login_and_switch(app_client, attacker)
    resp = await app_client.post(
        "/crm/categorias/guardar",
        headers=headers,
        data={"name": f"Nueva categoría atacante {uuid.uuid4().hex[:6]}", "parent_id": str(parent_id)},
    )
    assert resp.status_code == 303
    assert "danger" in resp.headers["location"], (
        "un parent_id de otro tenant debe rechazarse, no crear la categoría igual"
    )

    async with tenant_scope(attacker):
        async with get_session() as session:
            count = (
                await session.execute(select(func.count()).select_from(Category))
            ).scalar()
            assert count == 0, "el intento rechazado no debe dejar una categoría huérfana"

    async with tenant_scope(moto):
        async with get_session() as session:
            await session.execute(Category.__table__.delete().where(Category.id == parent_id))
