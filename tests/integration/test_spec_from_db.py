"""AgentSpec desde BD contra el stack real (Sprint 2, Fase A)."""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_load_active_spec_desde_bd(moto_setup) -> None:
    _t1, t2 = moto_setup
    from core.agents.spec import load_active_spec
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t2):
        spec = await load_active_spec()
    assert spec.tenantId == str(t2)
    assert spec.persona.tone == "cercano"
    # el spec sembrado referencia los playbooks default + los del pack habilitado
    assert {p.name for p in spec.playbooks} == {
        "identidad",
        "tono",
        "reglas-operacion",
        "respuestas",
        "ventas",
        "seguimiento",
    }


async def test_spec_aislado_por_tenant(tenant_setup) -> None:
    t1, t2 = tenant_setup
    from agentcore_contracts import AgentSpec

    from core.agents.spec import load_active_spec
    from core.shared.tenant import tenant_scope

    # el spec de T1 y T2 son distintos y quedan aislados por RLS
    async with tenant_scope(t1):
        spec_t1 = await load_active_spec()
    async with tenant_scope(t2):
        spec_t2 = await load_active_spec()
    assert isinstance(spec_t1, AgentSpec) and isinstance(spec_t2, AgentSpec)
    assert spec_t1.tenantId == str(t1)
    assert spec_t2.tenantId == str(t2)
    assert spec_t1.tenantId != spec_t2.tenantId


async def test_enablement_packs_por_tenant(moto_setup, tenant_setup) -> None:
    """ADR-0019: un tenant con pack lo tiene habilitado; el core siempre; otro sin packs no."""
    _t1, moto = moto_setup
    _a, t2 = tenant_setup
    from core.agents.spec import enabled_packs, load_active_spec
    from core.shared.tenant import tenant_scope

    async with tenant_scope(moto):
        spec_moto = await load_active_spec()
    async with tenant_scope(t2):
        spec_t2 = await load_active_spec()

    packs_moto = enabled_packs(spec_moto)
    packs_t2 = enabled_packs(spec_t2)
    assert "core" in packs_moto and "core" in packs_t2
    assert "pack_commerce" in packs_moto, "el tenant con el pack lo debe tener habilitado"
    assert "pack_commerce" not in packs_t2, "un tenant sin packs no debe tener el CRM"