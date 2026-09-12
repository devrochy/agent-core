"""Salud de plugin por tenant (ADR-0024): el test de regresión central del
incidente real (tenant `yopoldo`, 2026-09-11/12, `docs/ROADMAP.md`
filas 143-149) -- una excepción de runtime causada por UN tenant apagaba
el plugin para TODOS los tenants (`auto_disable` -> `deactivate` ->
`mcp_host.remove_plugin`, sin scope de tenant). Ahora la tool sigue
registrada en `mcp_host` para todos; solo el tenant que causó el error
pierde acceso a ella (hasta que un admin la reactive).
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID, TEST_BETA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID
T2 = TEST_BETA_ID


async def test_disabled_plugins_for_tenant_no_cruza_tenant():
    """Aislamiento cross-tenant de `plugin_tenant_state` (mismo patrón que
    `test_pack_commerce_order_confirmation.py::test_confirmar_cotizacion_no_ve_cotizacion_de_otro_tenant`)."""
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        await plugin_manager.auto_disable_for_tenant(T1, "pack_commerce", "boom de prueba")

    async with tenant_scope(T1):
        assert await plugin_manager.disabled_plugins_for_tenant(T1) == {"pack_commerce"}
        assert await plugin_manager.is_disabled_for_tenant(T1, "pack_commerce") is True

    async with tenant_scope(T2):
        assert await plugin_manager.disabled_plugins_for_tenant(T2) == set()
        assert await plugin_manager.is_disabled_for_tenant(T2, "pack_commerce") is False

    # Reactivación (acción de admin, invariante #8: marca deleted_at, no
    # borra la fila) -- vuelve a "sano".
    async with tenant_scope(T1):
        await plugin_manager.reactivate_for_tenant(T1, "pack_commerce")
        assert await plugin_manager.disabled_plugins_for_tenant(T1) == set()

    # Si el mismo plugin vuelve a fallar para este tenant, se reutiliza la
    # misma fila (mismo PK) -- no revienta con una violación de unicidad.
    async with tenant_scope(T1):
        await plugin_manager.auto_disable_for_tenant(T1, "pack_commerce", "boom de nuevo")
        assert await plugin_manager.disabled_plugins_for_tenant(T1) == {"pack_commerce"}
        await plugin_manager.reactivate_for_tenant(T1, "pack_commerce")


class _ToolRota:
    """Skill de prueba registrada bajo `plugin_id="pack_commerce"` (no
    "core" -- `loop.py` excluye explícitamente al core de auto_disable) con
    una tool cuyo handler siempre revienta."""

    name = "skill_de_prueba_rota"
    description = "skill de prueba"
    keywords: list[str] = []  # noqa: RUF012

    async def _handler(self, arguments, context):
        raise RuntimeError("boom real de una tool")

    def get_tools(self):
        from core.mcp.types import MCPTool

        return [
            MCPTool(
                name="tool_de_prueba_rota",
                description="siempre falla, para el test de auto_disable_for_tenant",
                parameters={"type": "object", "properties": {}},
                handler=self._handler,
            )
        ]


@pytest.fixture
def _registered_failing_tool():
    from core.mcp.host import mcp_host

    skill = _ToolRota()
    mcp_host.begin_activation("pack_commerce")
    mcp_host.register_skill(skill)
    mcp_host.end_activation()
    try:
        yield
    finally:
        mcp_host._skills.pop("skill_de_prueba_rota", None)
        mcp_host._tools.pop("tool_de_prueba_rota", None)


async def test_tool_rota_solo_afecta_al_tenant_que_fallo(_registered_failing_tool):
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.llm.types import LLMToolCall
    from core.mcp.host import mcp_host
    from core.orchestrator.loop import _execute_tool_safely
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tool_call = LLMToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name="tool_de_prueba_rota", arguments={})

    async with tenant_scope(T1):
        async with get_session() as session:
            customer = Customer(
                tenant_id=T1,
                channel="whatsapp",
                external_id=f"whatsapp:test-{uuid.uuid4().hex[:8]}",
            )
            session.add(customer)
            await session.flush()
            conversation = Conversation(tenant_id=T1, customer_id=customer.id, channel="whatsapp")
            session.add(conversation)
            await session.flush()

        result = await _execute_tool_safely(conversation, customer, tool_call)

    assert "error" in result, result

    # T1 (el que falló) queda marcado; T2 (que nunca llamó la tool) no.
    async with tenant_scope(T1):
        assert await plugin_manager.is_disabled_for_tenant(T1, "pack_commerce") is True
    async with tenant_scope(T2):
        assert await plugin_manager.is_disabled_for_tenant(T2, "pack_commerce") is False

    # La tool sigue registrada en mcp_host para TODOS -- a diferencia del
    # comportamiento viejo (`auto_disable` -> `deactivate` ->
    # `mcp_host.remove_plugin`), que la habría borrado para todos.
    assert mcp_host.get_tool("tool_de_prueba_rota") is not None

    # Limpieza para no dejar T1 marcado entre corridas de la suite.
    async with tenant_scope(T1):
        await plugin_manager.reactivate_for_tenant(T1, "pack_commerce")
