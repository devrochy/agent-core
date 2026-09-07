"""Aislamiento multi-tenant genérico (ADR-0001/0018) contra el stack real.

Un solo test por propiedad, usando DOS tenants genéricos (t1 y tenant-moto):
los asserts no dependen del cliente ni de un tenant concreto — crecer en tenants
nunca exige añadir tests.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

MOTO_CATALOG_SIZE = 3  # tests/fixtures/tenants/moto/catalog.json


async def _product_count(tenant_id) -> int:
    from pack_commerce.models import Product

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return (
                await session.execute(select(func.count()).select_from(Product))
            ).scalar()


async def test_catalogo_aislado_por_tenant(moto_setup) -> None:
    """Cada tenant ve SOLO su catálogo (RLS + filtro por tenant, ADR-0018)."""
    t1, moto = moto_setup
    assert await _product_count(moto) == MOTO_CATALOG_SIZE, "el tenant de ejemplo tiene su catálogo"

    # el SKU del tenant de ejemplo no existe en t1
    from pack_commerce.models import ProductVariant

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        async with get_session() as session:
            leak = (
                await session.execute(
                    select(func.count())
                    .select_from(ProductVariant)
                    .where(ProductVariant.sku.like("TST-%"))
                )
            ).scalar()
    assert leak == 0, "los productos de un tenant no filtran hacia otro"


async def test_spec_enablement_y_escalacion_por_tenant(moto_setup) -> None:
    """El enablement de packs y los triggers de escalación son por tenant."""
    t1, moto = moto_setup
    from core.agents.spec import enabled_packs, load_active_spec
    from core.shared.tenant import tenant_scope

    async with tenant_scope(moto):
        spec = await load_active_spec()
    assert "pack_commerce" in enabled_packs(spec), "el tenant de ejemplo tiene el pack habilitado"
    assert "compatibilidad" in spec.policies.escalation.triggers

    # t1 no debe haberse contagiado el trigger específico del otro tenant
    async with tenant_scope(t1):
        spec_t1 = await load_active_spec()
    assert "compatibilidad" not in spec_t1.policies.escalation.triggers


async def test_numeracion_independiente_por_tenant(moto_setup) -> None:
    """ADR-0018: cada tenant arranca su numeración de pedidos en 1 (sin colisión)."""
    t1, moto = moto_setup
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    for tid in (t1, moto):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text("delete from pack_commerce_order_counters where tenant_id = :t"),
                    {"t": tid},
                )
    async with tenant_scope(t1):
        async with get_session() as session:
            s1 = (
                await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": t1})
            ).scalar()
    async with tenant_scope(moto):
        async with get_session() as session:
            s2 = (
                await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": moto})
            ).scalar()
    assert s1 == 1 and s2 == 1, "ambos tenants arrancan su numeración en 1, de forma independiente"


async def test_onboarding_idempotente(moto_setup) -> None:
    """Correr el onboarding dos veces no duplica filas."""
    from core.db.models import AgentSpecRecord, AppSettings, Tenant
    from core.db.session import get_session
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup

    async def _counts() -> tuple[int, int, int]:
        async with get_session() as session:
            tenants = (
                await session.execute(select(func.count()).select_from(Tenant).where(Tenant.slug == "tenant-moto"))
            ).scalar()
        async with tenant_scope(moto):
            async with get_session() as session:
                specs = (
                    await session.execute(
                        select(func.count())
                        .select_from(AgentSpecRecord)
                        .where(AgentSpecRecord.tenant_id == moto)
                    )
                ).scalar()
                settings = (
                    await session.execute(
                        select(func.count())
                        .select_from(AppSettings)
                        .where(AppSettings.tenant_id == moto)
                    )
                ).scalar()
        return tenants, specs, settings

    antes = await _counts()
    await onboard_tenant(
        name="Tenant de ejemplo (moto)",
        slug="tenant-moto",
        business_description="Idempotente",
        packs=["pack_commerce"],
    )
    despues = await _counts()
    assert antes == despues, "el onboarding repetido no duplica filas"


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def test_panel_lista_tenants_y_crea_uno_nuevo(app_client, moto_setup) -> None:
    """El panel (solo master) lista tenants y el wizard crea uno nuevo."""
    _t1, _moto = moto_setup
    cookie = await _login(app_client)
    headers = {"Cookie": cookie}

    r = await app_client.get("/admin/tenants", headers=headers)
    assert r.status_code == 200
    assert "tenant-moto" in r.text, "la lista muestra el tenant de ejemplo"

    r_form = await app_client.get("/admin/tenants/nuevo", headers=headers)
    assert r_form.status_code == 200
    assert "Nuevo tenant" in r_form.text

    r = await app_client.post(
        "/admin/tenants/nuevo",
        headers=headers,
        data={
            "name": "Panadería del Barrio",
            "business": "Panadería de ejemplo",
            "language": "es",
            "tone": "cercano",
            "escalation_triggers": "asesor, queja",
        },
    )
    assert r.status_code == 303
    assert "ac_tenant" in r.headers.get("set-cookie", ""), "el tenant nuevo se activa en la cookie"
    assert "panaderia-del-barrio" in r.headers["location"], "redirige al spec con flash"

    # slug duplicado: idempotente, no rompe
    r2 = await app_client.post(
        "/admin/tenants/nuevo",
        headers=headers,
        data={"name": "Panadería del Barrio", "slug": "panaderia-del-barrio"},
    )
    assert r2.status_code == 303