"""`run_guardrails` debe poder desactivar un guardrail INDIVIDUAL por
nombre (parametrización por tenant, `AppSettings.behavior_config
["disabled_guardrails"]`), sin depender de apagar el pack entero
(`enabled`, ya existente para ADR-0019).

Sin mocks de BD: se monkeypatchea `mcp_host.get_guardrails` directamente,
mismo criterio que `test_plugin_manager_tenant_state.py`.
"""

from __future__ import annotations

import pytest

import core.orchestrator.guardrails as guardrails_mod
from core.mcp.types import Guardrail, GuardrailResult


async def _passing(_text, _results):
    return GuardrailResult(passed=True)


async def _failing(_text, _results):
    return GuardrailResult(passed=False, reason="siempre falla")


def _fake_guardrails():
    return [
        Guardrail(name="crm_amount_check", fn=_failing, priority=5, plugin_id="pack_commerce"),
        Guardrail(name="crm_completion_check", fn=_failing, priority=6, plugin_id="pack_commerce"),
        Guardrail(name="otro_pack_guardrail", fn=_passing, priority=10, plugin_id="pack_calendar"),
    ]


@pytest.fixture(autouse=True)
def _fake_host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(guardrails_mod.mcp_host, "get_guardrails", _fake_guardrails)


@pytest.mark.asyncio
async def test_sin_disabled_corren_todos_los_guardrails_del_pack_habilitado():
    verdict = await guardrails_mod.run_guardrails("hola", [], enabled={"pack_commerce"})
    assert verdict.passed is False
    assert "crm_amount_check" in verdict.reason


@pytest.mark.asyncio
async def test_disabled_omite_el_guardrail_puntual():
    """Con `crm_amount_check` desactivado, el siguiente guardrail habilitado
    del mismo pack (`crm_completion_check`, también fallando en este fake)
    sigue corriendo -- desactivar uno no apaga el pack entero."""
    verdict = await guardrails_mod.run_guardrails(
        "hola", [], enabled={"pack_commerce"}, disabled={"crm_amount_check"}
    )
    assert verdict.passed is False
    assert "crm_completion_check" in verdict.reason
    assert "crm_amount_check" not in verdict.reason


@pytest.mark.asyncio
async def test_disabled_todos_los_guardrails_del_pack_pasa():
    verdict = await guardrails_mod.run_guardrails(
        "hola",
        [],
        enabled={"pack_commerce"},
        disabled={"crm_amount_check", "crm_completion_check"},
    )
    assert verdict.passed is True


def test_register_guardrail_nombre_duplicado_revienta():
    """Hallazgo de `auditor-multitenant`: si dos packs pudieran registrar
    un guardrail con el MISMO nombre, el toggle individual por tenant (que
    filtra por nombre, no por `(plugin_id, name)`) apagaría el de ambos
    packs sin que el admin se entere -- mismo criterio de unicidad que ya
    exige `register_skill`/`register_tool` para tools y skills."""
    from core.mcp.host import MCPHost
    from core.shared.exceptions import PluginError

    host = MCPHost()
    host.begin_activation("pack_a")
    host.register_guardrail("mismo_nombre", _passing, priority=1)
    host.end_activation()

    host.begin_activation("pack_b")
    with pytest.raises(PluginError, match="Guardrail duplicado"):
        host.register_guardrail("mismo_nombre", _passing, priority=1)
    host.end_activation()


@pytest.mark.asyncio
async def test_disabled_no_afecta_guardrails_de_otro_pack():
    """`disabled` es un set de NOMBRES -- no debe afectar un guardrail de
    otro pack que casualmente esté habilitado."""
    verdict = await guardrails_mod.run_guardrails(
        "hola",
        [],
        enabled={"pack_commerce", "pack_calendar"},
        disabled={"crm_amount_check", "crm_completion_check"},
    )
    assert verdict.passed is True  # otro_pack_guardrail pasa (fake _passing)
