"""`crm_completion_check` resuelve `completion_claims_disabled`/
`completion_claims_custom` desde la config REAL del tenant (ADR-0025),
no solo mockeada -- complementa los tests puros de
`tests/test_pack_commerce_completion_guardrail.py`.
"""

from __future__ import annotations

import os

import pytest
from conftest import TEST_ALPHA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


def _tool_result(name: str, **fields) -> dict:
    return {**fields, "_tool_name": name}


async def _delete_pack_commerce_config() -> None:
    """No existía fila real de `pack_commerce` para T1 antes de este test
    (confirmado contra la BD local) -- se borra al final en vez de
    "resetear a vacío" para no dejar ninguna fila nueva en la BD
    compartida de tests."""
    from sqlalchemy import delete

    from core.db.models import PluginConfig
    from core.db.session import get_session

    async with get_session() as session:
        await session.execute(
            delete(PluginConfig).where(
                PluginConfig.plugin_id == "pack_commerce", PluginConfig.tenant_id == T1
            )
        )


async def test_completion_claims_disabled_desde_config_real():
    from pack_commerce.module import CRMModule

    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    plugin_manager.scan()

    async with tenant_scope(T1):
        try:
            await plugin_manager.save_config(
                "pack_commerce", {"completion_claims_disabled": "link_de_pago"}
            )

            module = CRMModule()
            verdict = await module._completion_guardrail("Te envío el link de pago ahora mismo.", [])
            assert verdict.passed, verdict.reason

            # otro claim sigue vigente -- desactivar uno no apaga los demás.
            verdict2 = await module._completion_guardrail("Cerré tu pedido, ya está confirmado.", [])
            assert not verdict2.passed
        finally:
            await _delete_pack_commerce_config()


async def test_completion_claims_custom_desde_config_real():
    from pack_commerce.module import CRMModule

    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    plugin_manager.scan()

    async with tenant_scope(T1):
        try:
            await plugin_manager.save_config(
                "pack_commerce",
                {"completion_claims_custom": "agenda_cita: agendé tu cita => crear_cita"},
            )

            module = CRMModule()
            verdict = await module._completion_guardrail("Listo, ya agendé tu cita.", [])
            assert not verdict.passed
            assert "agenda_cita" in verdict.reason

            verdict2 = await module._completion_guardrail(
                "Listo, ya agendé tu cita.", [_tool_result("crear_cita", status="ok")]
            )
            assert verdict2.passed, verdict2.reason
        finally:
            await _delete_pack_commerce_config()
