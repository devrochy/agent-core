"""brand_voice obligatoria: seed desde voz-de-marca.md y aviso cuando falta (#158)."""

from __future__ import annotations

import os

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_ensure_brand_voice_siembra_desde_voz_de_marca() -> None:
    from sqlalchemy import select

    from core.alta_negocio import onboard_tenant
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.onboarding import ensure_brand_voice, tenant_dir
    from core.shared.tenant import tenant_scope

    slug = uniq("bv")
    tenant_id, _ = await onboard_tenant(
        name=f"BrandVoice {slug}",
        slug=slug,
        business_description="Negocio con voz de marca.",
        persona={"tone": "cercano"},
    )
    docs = tenant_dir(slug) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "voz-de-marca.md").write_text(
        "Somos SolversHub. Los precios salen SOLO de las herramientas, nunca se inventan.",
        encoding="utf-8",
    )

    seeded = await ensure_brand_voice(tenant_id, slug)
    assert seeded is True

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            st = (
                await session.execute(
                    select(AppSettings).where(AppSettings.tenant_id == tenant_id)
                )
            ).scalar_one()
            assert "precios salen SOLO" in st.brand_voice

    # Idempotente: ya tiene voz → no re-siembra.
    seeded2 = await ensure_brand_voice(tenant_id, slug)
    assert seeded2 is False


async def test_ensure_brand_voice_sin_archivo_no_falla() -> None:
    from core.alta_negocio import onboard_tenant
    from core.onboarding import ensure_brand_voice

    slug = uniq("bvnofile")
    tenant_id, _ = await onboard_tenant(
        name=f"SinVoz {slug}",
        slug=slug,
        business_description="Sin archivo de voz.",
        persona={"tone": "formal"},
    )
    assert await ensure_brand_voice(tenant_id, slug) is False


def test_identity_out_marca_warning_si_vacio() -> None:
    from agentcore_contracts import AgentSpec

    from core.agents.spec import DEFAULT_SPEC
    from core.api.routers.settings import _identity_out

    spec = AgentSpec.model_validate(DEFAULT_SPEC)
    out = _identity_out(None, spec, available=[])
    assert out.brand_voice == ""
    assert out.brand_voice_warning is True