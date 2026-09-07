"""Playbooks de un pack cargados desde el AgentSpec del tenant (allowlist, ADR-0014)."""

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


async def test_playbooks_pack_cargados_por_allowlist(moto_setup) -> None:
    _t1, t2 = moto_setup
    from core.agents.playbooks import load_playbook
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t2):
        ventas = await load_playbook(t2, "ventas")
        assert ventas is not None
        assert "Proceso de venta" in ventas
        assert not ventas.lstrip().startswith("---"), "el frontmatter no debe filtrarse"

        seguimiento = await load_playbook(t2, "seguimiento")
        assert seguimiento is not None and "reenganche" in seguimiento

        # fuera del índice del tenant: rechazado
        assert await load_playbook(t2, "no-existe") is None


async def test_playbook_ventas_entra_al_ensamblador(moto_setup) -> None:
    _t1, t2 = moto_setup
    from core.agents.spec import load_active_spec
    from core.llm.prompt_builder import PromptContext, build_system_blocks
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t2):
        spec = await load_active_spec()
        blocks = await build_system_blocks(
            spec,
            PromptContext(assistant_name="Tenant", business_description="Tienda"),
        )
    joined = "\n".join(blocks)
    assert "Proceso de venta" in joined
    assert "NUNCA inventes precios" in joined