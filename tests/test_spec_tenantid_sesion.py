"""El negocio de un turno sale del `tenant_scope`, nunca del `tenantId` del AgentSpec.

Hallazgo del auditor multi-tenant (2026-10-10): el editor crudo guardaba el `tenantId`
que trajera el JSON, y el prompt resolvía el slug desde ahí, así que un negocio podía
cargar los playbooks y el flow.json (`TENANTS_ROOT/<slug>/`) de OTRO negocio.

Unit tests sin BD: `tenant_slug` (consulta a `tenants`) y `tenant_dir` se reemplazan.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agentcore_contracts import AgentSpec

import core.agents.flow as flow_mod
import core.agents.spec as spec_mod
import core.onboarding as onboarding
import core.shared.tenant as tenant_mod
from core.agents.playbooks import load_playbook
from core.llm.prompt_builder import PromptContext, build_system_blocks
from core.shared.tenant import tenant_scope

pytestmark = pytest.mark.aislamiento

PROPIO = uuid.UUID("11111111-1111-1111-1111-111111111111")
AJENO = uuid.UUID("22222222-2222-2222-2222-222222222222")
SLUGS = {PROPIO: "negocio-propio", AJENO: "negocio-ajeno"}
SECRETO_AJENO = "Guion confidencial del negocio ajeno."


def make_spec(tenant_id: uuid.UUID | str) -> AgentSpec:
    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId=str(tenant_id),
        persona={
            "role": "asistente",
            "tone": "cercano",
            "warmth": 3,
            "emojis": "sparse",
            "maxLength": 500,
            "forbiddenPhrases": [],
        },
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=[{"name": "tono", "description": "Tono"}],
        tools=[],
        policies={"maxToolCallsPerTurn": 10},
        packs=[],
    )


@pytest.fixture
def negocios(tmp_path, monkeypatch) -> list[uuid.UUID]:
    """Dos negocios en disco; solo el AJENO tiene override de `tono`.

    Devuelve los ids cuyo slug se consultó, para comprobar de dónde salió.
    """
    consultados: list[uuid.UUID] = []

    async def _slug(tenant_id: uuid.UUID) -> str | None:
        consultados.append(tenant_id)
        return SLUGS.get(tenant_id)

    ajeno_dir = tmp_path / SLUGS[AJENO] / "playbooks"
    ajeno_dir.mkdir(parents=True)
    (ajeno_dir / "tono.md").write_text(SECRETO_AJENO, encoding="utf-8")
    monkeypatch.setattr(tenant_mod, "tenant_slug", _slug)
    monkeypatch.setattr(onboarding, "tenant_dir", lambda s: tmp_path / s)
    return consultados


# ------------------------------------------------------------------ lectores
@pytest.mark.asyncio
async def test_prompt_no_carga_playbooks_del_tenantid_ajeno(negocios) -> None:
    async with tenant_scope(PROPIO):
        blocks = await build_system_blocks(
            make_spec(AJENO), PromptContext(assistant_name="A", business_description="B")
        )
    assert SECRETO_AJENO not in "\n".join(blocks)
    assert negocios == [PROPIO]


@pytest.mark.asyncio
async def test_prompt_del_negocio_ajeno_si_ve_su_override(negocios) -> None:
    """Control: el override existe y se carga para su dueño (spec correcto, sin cambio)."""
    async with tenant_scope(AJENO):
        blocks = await build_system_blocks(
            make_spec(AJENO), PromptContext(assistant_name="A", business_description="B")
        )
    assert SECRETO_AJENO in "\n".join(blocks)


@pytest.mark.asyncio
async def test_load_playbook_resuelve_slug_desde_la_sesion(negocios) -> None:
    async with tenant_scope(PROPIO):
        content = await load_playbook(make_spec(AJENO), "tono")
    assert content is not None
    assert SECRETO_AJENO not in content
    assert negocios == [PROPIO]


@pytest.mark.asyncio
async def test_fuera_de_scope_no_hay_override_aunque_el_spec_traiga_tenantid(negocios) -> None:
    content = await load_playbook(make_spec(AJENO), "tono")
    assert content is not None
    assert SECRETO_AJENO not in content
    assert negocios == []


@pytest.mark.asyncio
async def test_flow_carga_el_flow_json_del_negocio_de_la_sesion(negocios, monkeypatch) -> None:
    pedidos: list[str] = []

    def _flow(slug: str) -> None:
        pedidos.append(slug)
        return None

    monkeypatch.setattr(flow_mod, "load_tenant_flow", _flow)
    async with tenant_scope(PROPIO):
        await flow_mod.resolve_flow(make_spec(AJENO))
    assert pedidos == [SLUGS[PROPIO]]


# ------------------------------------------------------------------ escritura
def test_con_tenant_de_la_fila_reemplaza_el_ajeno() -> None:
    assert spec_mod.con_tenant_de_la_fila(make_spec(AJENO), PROPIO).tenantId == str(PROPIO)
    correcto = make_spec(PROPIO)
    assert spec_mod.con_tenant_de_la_fila(correcto, PROPIO) is correcto


@pytest.mark.asyncio
async def test_save_spec_if_changed_fuerza_el_tenant_que_guarda(monkeypatch) -> None:
    guardados: list[tuple[AgentSpec, Any]] = []

    async def _save(new_spec: AgentSpec, tenant_id: Any = None) -> str:
        guardados.append((new_spec, tenant_id))
        return "1.0.1"

    monkeypatch.setattr(spec_mod, "save_spec_version", _save)
    data = make_spec(AJENO).model_dump(mode="json")
    data["persona"]["maxLength"] = 300
    assert await spec_mod.save_spec_if_changed(make_spec(PROPIO), data, PROPIO) == "1.0.1"
    [(nuevo, tid)] = guardados
    assert nuevo.tenantId == str(PROPIO)
    assert tid == PROPIO


@pytest.mark.asyncio
async def test_save_spec_if_changed_usa_el_negocio_del_scope(monkeypatch) -> None:
    guardados: list[AgentSpec] = []

    async def _save(new_spec: AgentSpec, tenant_id: Any = None) -> str:
        guardados.append(new_spec)
        return "1.0.1"

    monkeypatch.setattr(spec_mod, "save_spec_version", _save)
    data = make_spec(AJENO).model_dump(mode="json")
    data["persona"]["maxLength"] = 300
    async with tenant_scope(PROPIO):
        await spec_mod.save_spec_if_changed(make_spec(PROPIO), data)
    assert [s.tenantId for s in guardados] == [str(PROPIO)]


@pytest.mark.asyncio
async def test_cambiar_solo_el_tenantid_no_crea_version(monkeypatch) -> None:
    async def _save(*_a: Any, **_k: Any) -> str:
        raise AssertionError("no debía guardar")

    monkeypatch.setattr(spec_mod, "save_spec_version", _save)
    data = make_spec(AJENO).model_dump(mode="json")
    assert await spec_mod.save_spec_if_changed(make_spec(PROPIO), data, PROPIO) is None


@pytest.mark.asyncio
async def test_load_playbook_rechaza_un_negocio_distinto_del_scope() -> None:
    async with tenant_scope(PROPIO):
        with pytest.raises(PermissionError):
            await load_playbook(AJENO, "tono")
