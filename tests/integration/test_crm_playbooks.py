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


async def test_playbook_override_por_tenant_gana_y_no_deja_residuo(moto_setup) -> None:
    """ADR-0026: `TENANTS_ROOT/<slug>/playbooks/<name>.md`, si existe, gana
    sobre el default del pack para ESE tenant; sin el archivo, vuelve a
    verse el default -- no queda nada "pegado" entre corridas."""
    _t1, moto = moto_setup
    from core.agents.playbooks import load_playbook
    from core.onboarding import tenant_dir
    from core.shared.tenant import tenant_scope

    MOTO_SLUG = "tenant-moto"
    override_dir = tenant_dir(MOTO_SLUG) / "playbooks"
    override_path = override_dir / "ventas.md"
    override_dir.mkdir(parents=True, exist_ok=True)
    override_path.write_text("Guion de ventas a medida de tenant-moto.", encoding="utf-8")
    try:
        async with tenant_scope(moto):
            content = await load_playbook(moto, "ventas")
        assert content == "Guion de ventas a medida de tenant-moto."
    finally:
        override_path.unlink()
        override_dir.rmdir()  # solo el archivo del test, el directorio queda vacío igual

    # Sin override propio: vuelve a ver el default del pack, no algo cacheado.
    async with tenant_scope(moto):
        default_content = await load_playbook(moto, "ventas")
    assert default_content is not None
    assert "Proceso de venta" in default_content
    assert "a medida de tenant-moto" not in default_content


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