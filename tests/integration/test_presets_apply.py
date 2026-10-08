"""HU-FWK-03 / ADR-0110: alta con preset contra la BD real.

Gherkin: alta con preset, packs con claves, fragmentos por pack, idempotencia.
"""

from __future__ import annotations

import os

import pytest
from _api_helpers import uniq
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _alta(preset: str, plan: str | None):
    from core.onboarding import onboard_tenant

    slug = uniq("preset")
    return await onboard_tenant(name=f"Negocio {slug}", slug=slug, preset=preset, plan=plan)


async def _estado(tid):
    from pack_commerce.models import Product, ProductVariant

    from core.agents.spec import load_active_spec
    from core.db.models import AgentSpecRecord, Tenant
    from core.db.session import get_session
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        spec = await load_active_spec()
        async with get_session() as s:
            productos = (await s.execute(select(func.count()).select_from(Product).where(Product.tenant_id == tid))).scalar_one()
            skus = set((await s.execute(select(ProductVariant.sku).where(ProductVariant.tenant_id == tid))).scalars())
            versiones = (await s.execute(select(func.count()).select_from(AgentSpecRecord).where(AgentSpecRecord.tenant_id == tid))).scalar_one()
            tenant = await s.get(Tenant, tid)
        config = await plugin_manager.load_config("pack_commerce") if "pack_commerce" in spec.packs else None
    return spec, productos, skus, versiones, (tenant.settings or {}).get("preset"), config


@pytest.mark.asyncio
async def test_alta_con_preset_restaurante() -> None:
    """Gherkin «Alta con preset», «Packs con claves» y «Fragmentos por pack»."""
    tid, pasos = await _alta("restaurante", "basic")
    spec, productos, skus, versiones, preset, config = await _estado(tid)

    assert "pack_commerce" in spec.packs
    # pack_payments exige claves de Wompi: queda pendiente y el alta no falla.
    assert "pack_payments" not in spec.packs
    assert [p["pack"] for p in preset["pendientes"]] == ["pack_payments"]
    assert preset["id"] == "restaurante" and preset["version"] == 1
    # Catálogo de ejemplo desde `seed_data` del pack y config del fragmento.
    assert {"MENU-PLATO-DIA", "MENU-LIMONADA"} <= skus and productos == 2
    assert config.order_prefix == "PED"
    assert versiones >= 2  # versión nueva del AgentSpec
    assert {p.name for p in spec.playbooks} >= {"ventas"}
    assert any("pendiente de configurar" in p for p in pasos)


@pytest.mark.asyncio
async def test_aplicar_dos_veces_no_duplica() -> None:
    """Gherkin «Idempotencia»."""
    from core.presets.apply import apply_preset

    tid, _ = await _alta("restaurante", "basic")
    antes = await _estado(tid)
    resultado = await apply_preset(tid, "restaurante")
    despues = await _estado(tid)

    assert resultado.spec_version is None  # el spec no cambió
    assert despues[1] == antes[1] and despues[2] == antes[2]
    assert despues[3] == antes[3]
    assert despues[0].packs == antes[0].packs


@pytest.mark.asyncio
async def test_no_pisa_la_config_que_el_negocio_cambio() -> None:
    from core.plugin_manager import plugin_manager
    from core.presets.apply import apply_preset
    from core.shared.tenant import tenant_scope

    tid, _ = await _alta("restaurante", "basic")
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_commerce", {"order_prefix": "MIO"})
    await apply_preset(tid, "restaurante")
    *_, config = await _estado(tid)
    assert config.order_prefix == "MIO"


@pytest.mark.asyncio
async def test_plan_insuficiente_no_cambia_nada() -> None:
    from core.presets import PresetError

    with pytest.raises(PresetError, match="plan más alto"):
        await _alta("cobranza", None)  # free < premium
