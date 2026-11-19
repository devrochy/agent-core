"""Resolución de completion_claims por config de tenant en pack-calendar (#157)."""

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


async def test_resolved_claims_aplica_disabled_y_custom() -> None:
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    slug = uniq("calclaims")
    tenant_id, _ = await onboard_tenant(
        name=f"Claims {slug}",
        slug=slug,
        business_description="Citas de prueba.",
        persona={"tone": "cercano"},
        packs=["pack_calendar"],
    )

    async with tenant_scope(tenant_id):
        await plugin_manager.save_config(
            "pack_calendar",
            {
                "source": "interno",
                "completion_claims_disabled": "cancelada",
                "completion_claims_custom": "mi_claim: te confirmo la cita => create_calendar_event",
            },
        )
        from pack_calendar.module import GoogleCalendarModule

        module = GoogleCalendarModule()
        resolved = await module._resolved_completion_claims()
        assert "cancelada" not in resolved
        assert "mi_claim" in resolved
        assert "agendada" in resolved
        pattern, tools = resolved["mi_claim"]
        assert tools == {"create_calendar_event"}
        assert pattern.search("Te confirmo la cita")