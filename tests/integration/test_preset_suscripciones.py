"""HU-SH-01 · preset `suscripciones`, precio desde core/billing y voz de marca, contra la BD real."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Planes"]],
    "products": [
        {
            "name": "Basic",
            "description": "Plan Basic",
            "category_path": ["Planes"],
            "variants": [
                {
                    "sku": "PLAN-BASIC",
                    "price": 1,  # se ignora: manda core/billing/plans.py
                    "stock": 0,
                    "attributes": {"periodicidad": "mensual", "trial_dias": 0, "precio_de_plan": "basic"},
                }
            ],
        }
    ],
}


async def _variante(tid: uuid.UUID, sku: str):
    from pack_commerce.models import ProductVariant

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return (
                await s.execute(select(ProductVariant).where(ProductVariant.tenant_id == tid, ProductVariant.sku == sku))
            ).scalar_one()


async def test_el_precio_del_plan_sale_de_billing_y_se_resincroniza(monkeypatch) -> None:
    """Gherkin «Una fuente de precios»: cambiar Basic en plans.py cambia el producto."""
    from pack_commerce.jobs import _sincronizar_precios_de_plan
    from pack_commerce.seed import seed_catalog

    import core.billing.plans as plans
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(name="Agencia", slug=uniq("plan"), packs=["pack_commerce"])
    await seed_catalog(tid, CATALOGO)
    basic = plans.get_plan("basic")
    assert basic is not None
    assert Decimal((await _variante(tid, "PLAN-BASIC")).price) == Decimal(basic.price_cop)

    monkeypatch.setitem(plans.PLANS_BY_ID, "basic", basic.__class__(**{**basic.__dict__, "price_cop": 250_000}))
    assert await _sincronizar_precios_de_plan() >= 1
    assert Decimal((await _variante(tid, "PLAN-BASIC")).price) == Decimal(250_000)


async def test_preset_suscripciones_deja_el_proveedor_y_la_agenda_interna() -> None:
    """Gherkin «Reproducible»: packs del preset y proveedor `pack_subscriptions`."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope
    from core.subscriptions import proveedor_configurado

    tid, _ = await onboard_tenant(
        name="Agencia", slug=uniq("susc"), packs=[], preset="suscripciones", plan="premium"
    )
    async with tenant_scope(tid):
        spec = await load_active_spec()
        agenda = await plugin_manager.load_config("pack_calendar")
    packs = enabled_packs(spec)
    assert {"pack_commerce", "pack_calendar", "pack_subscriptions"} <= packs
    # Pagos necesita las llaves de Wompi del negocio: queda pendiente, no inventada.
    assert "pack_payments" not in packs
    assert agenda is not None and agenda.source == "interno"
    assert await proveedor_configurado(tid) == "pack_subscriptions"


async def test_voz_de_marca_se_reemplaza_solo_si_se_pide(tmp_path, monkeypatch) -> None:
    from core.alta_negocio import onboard_tenant
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.onboarding import ensure_brand_voice
    from core.shared.tenant import tenant_scope

    slug = uniq("voz")
    tid, _ = await onboard_tenant(name="Agencia", slug=slug, packs=["pack_commerce"])
    carpeta = tmp_path / slug / "docs"
    carpeta.mkdir(parents=True)
    monkeypatch.setenv("TENANTS_ROOT", str(tmp_path))
    import core.onboarding as onboarding

    monkeypatch.setattr(onboarding, "tenant_dir", lambda s: tmp_path / s)

    async def _voz() -> str:
        async with tenant_scope(tid):
            async with get_session() as s:
                fila = (await s.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one()
                return fila.brand_voice or ""

    (carpeta / "voz-de-marca.md").write_text("Versión 1", encoding="utf-8")
    await ensure_brand_voice(tid, slug)
    assert await _voz() == "Versión 1"
    (carpeta / "voz-de-marca.md").write_text("Versión 2: Free tiene 30 días de prueba", encoding="utf-8")
    assert await ensure_brand_voice(tid, slug) is False
    assert await _voz() == "Versión 1"
    assert await ensure_brand_voice(tid, slug, sobrescribir=True) is True
    assert await _voz() == "Versión 2: Free tiene 30 días de prueba"


async def test_aplicar_preset_a_un_negocio_existente_no_mezcla_ejemplos() -> None:
    """Un negocio con catálogo propio no recibe los productos de ejemplo del preset
    ni le pisan los atributos de un SKU que coincida (visto al aplicar HU-SH-01 en test)."""
    from pack_commerce.models import ProductVariant
    from pack_commerce.seed import seed_catalog

    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.presets.apply import apply_preset
    from core.shared.tenant import tenant_scope

    tid, _ = await onboard_tenant(name="Agencia", slug=uniq("exis"), packs=["pack_commerce"], plan="premium")
    catalogo = {
        **CATALOGO,
        "products": [
            {
                **CATALOGO["products"][0],
                "name": "Plan Free",
                "variants": [
                    {
                        "sku": "PLAN-FREE",
                        "price": 0,
                        "stock": 0,
                        "attributes": {"periodicidad": "mensual", "trial_dias": 30, "precio_de_plan": "free"},
                    }
                ],
            }
        ],
    }
    await seed_catalog(tid, catalogo)
    await apply_preset(tid, "suscripciones", sembrar_ejemplos=False)

    async with tenant_scope(tid):
        async with get_session() as s:
            variantes = (
                await s.execute(select(ProductVariant).where(ProductVariant.tenant_id == tid))
            ).scalars().all()
    assert [v.sku for v in variantes] == ["PLAN-FREE"]
    assert variantes[0].attributes.get("precio_de_plan") == "free"
