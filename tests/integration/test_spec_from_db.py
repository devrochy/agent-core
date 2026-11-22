"""Habilidades del negocio desde BD contra el stack real (ADR-0019, HU-AOS-91).

La carga del AgentSpec v2 desde `agent_specs` se retiró (ADR-0206): queda la habilitación de packs por negocio.
"""

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


async def test_enablement_packs_por_tenant(moto_setup, tenant_setup) -> None:
    """ADR-0019: un tenant con pack lo tiene habilitado; el core siempre; otro sin packs no."""
    _t1, moto = moto_setup
    _a, t2 = tenant_setup
    from core.agents.instalaciones_agente import packs_del_negocio
    from core.shared.tenant import tenant_scope

    # Las habilidades del negocio son las de sus agentes activos (HU-AOS-91).
    async with tenant_scope(moto):
        packs_moto = await packs_del_negocio(moto)
    async with tenant_scope(t2):
        packs_t2 = await packs_del_negocio(t2)
    assert "core" in packs_moto and "core" in packs_t2
    assert "pack_commerce" in packs_moto, "el tenant con el pack lo debe tener habilitado"
    assert "pack_commerce" not in packs_t2, "un tenant sin packs no debe tener el CRM"