"""Tests del flujo de atención por pack/tenant (ADR-0054). Unitarios, sin BD.

Se monkeypatchea el filesystem (tenant) y los loaders; la única lectura real es el
`flow.json` del pack-commerce, que debe existir y ser válido.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agentcore_contracts import AgentSpec

from core.agents import flow as flow_mod
from core.agents.flow import (
    FlowSection,
    ResolvedFlow,
    load_pack_flows,
    render_flow,
    resolve_flow,
)
from core.llm.prompt_builder import PromptContext, build_system_blocks


def make_spec(packs: list[str] | None = None) -> AgentSpec:
    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={
            "role": "asistente",
            "tone": "cercano",
            "warmth": 3,
            "emojis": "sparse",
            "maxLength": 500,
            "forbiddenPhrases": [],
        },
        model={"primary": "deepseek-chat", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=[],
        tools=[],
        policies={"maxToolCallsPerTurn": 10},
        packs=packs or [],
    )


def section(sid: str, tools: list[str], pack: str = "pack_commerce") -> FlowSection:
    return FlowSection(id=sid, label=sid.title(), purpose=f"propósito {sid}", pack=pack, tools=tools)


# ------------------------------------------------------------------ pack real
def test_pack_commerce_flow_existe_y_es_valido() -> None:
    sections = load_pack_flows(["pack_commerce"])
    ids = [s.id for s in sections]
    assert ids == ["saludo", "cotizacion", "pedido", "escalamiento"]
    tools = {t for s in sections for t in s.tools}
    assert {"consultar_inventario", "generar_cotizacion", "crear_pedido", "escalate_to_human"} <= tools


# ------------------------------------------------------------------ parseo
def test_parse_sections_descarta_malformadas_y_duplicadas(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    path.write_text(
        json.dumps(
            {
                "sections": [
                    {"id": "saludo", "label": "Saludo", "tools": ["consultar_inventario", "consultar_inventario"]},
                    {"id": "mal id!", "tools": ["x"]},
                    {"id": "saludo", "tools": ["otra"]},
                    {"id": "pedido", "tools": ["crear_pedido", "no-valido!"]},
                    "no soy dict",
                ]
            }
        ),
        encoding="utf-8",
    )
    sections = flow_mod._read_flow_file(path)
    assert sections is not None
    assert [s.id for s in sections] == ["saludo", "pedido"]
    assert sections[0].tools == ["consultar_inventario"]
    assert sections[1].tools == ["crear_pedido"]


def test_read_flow_file_json_invalido_devuelve_none(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    path.write_text("{no es json", encoding="utf-8")
    assert flow_mod._read_flow_file(path) is None


def test_tenant_flow_path_rechaza_slug_inseguro() -> None:
    with pytest.raises(ValueError):
        flow_mod.tenant_flow_path("../otro-tenant")
    with pytest.raises(ValueError):
        flow_mod.seed_tenant_flow("acme/../otro", ["pack_commerce"])


# ------------------------------------------------------------------ resolución
@pytest.mark.asyncio
async def test_resolve_flow_tenant_gana_sobre_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        flow_mod, "load_tenant_flow", lambda slug: [section("saludo", ["crear_lead"])]
    )
    monkeypatch.setattr(
        flow_mod, "load_pack_flows", lambda packs: [section("pedido", ["crear_pedido"])]
    )
    flow = await resolve_flow(make_spec(["pack_commerce"]), slug="acme")
    assert flow is not None
    assert flow.source == "tenant"
    assert flow.tool_names == ["crear_lead"]


@pytest.mark.asyncio
async def test_resolve_flow_cae_al_pack_si_no_hay_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: None)
    monkeypatch.setattr(
        flow_mod, "load_pack_flows", lambda packs: [section("pedido", ["crear_pedido"])]
    )
    flow = await resolve_flow(make_spec(["pack_commerce"]), slug="acme")
    assert flow is not None
    assert flow.source == "pack"
    assert flow.tool_names == ["crear_pedido"]


@pytest.mark.asyncio
async def test_resolve_flow_filtra_tools_no_disponibles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: None)
    monkeypatch.setattr(
        flow_mod,
        "load_pack_flows",
        lambda packs: [section("pedido", ["crear_pedido", "generar_link_pago"])],
    )
    flow = await resolve_flow(
        make_spec(["pack_commerce"]), slug="acme", available={"crear_pedido"}
    )
    assert flow is not None
    assert flow.tool_names == ["crear_pedido"]
    assert flow.unknown_tools == ["generar_link_pago"]


@pytest.mark.asyncio
async def test_resolve_flow_none_sin_secciones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: None)
    monkeypatch.setattr(flow_mod, "load_pack_flows", lambda packs: [])
    assert await resolve_flow(make_spec([]), slug="acme") is None


def test_render_flow_incluye_etapas_y_tools() -> None:
    flow = ResolvedFlow(
        sections=[section("saludo", ["crear_lead"])],
        source="pack",
    )
    text = render_flow(flow)
    assert "1. Saludo" in text
    assert "propósito saludo" in text
    assert "crear_lead" in text


# ------------------------------------------------------------------ siembra
def test_seed_tenant_flow_idempotente_y_preserva_propias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "acme" / "flow.json"
    monkeypatch.setattr(flow_mod, "tenant_flow_path", lambda slug: target)
    monkeypatch.setattr(
        flow_mod, "load_pack_flows", lambda packs: [section("saludo", ["crear_lead"])]
    )

    flow_mod.seed_tenant_flow("acme", ["pack_commerce"])
    first = json.loads(target.read_text(encoding="utf-8"))
    assert [s["id"] for s in first["sections"]] == ["saludo"]

    # El tenant agrega una sección propia y vuelve a sincronizar: no se pierde.
    first["sections"].append(
        {"id": "propia", "label": "Propia", "purpose": "", "pack": "tenant", "tools": ["mi_tool"]}
    )
    target.write_text(json.dumps(first), encoding="utf-8")
    flow_mod.seed_tenant_flow("acme", ["pack_commerce"])

    second = json.loads(target.read_text(encoding="utf-8"))
    assert [s["id"] for s in second["sections"]] == ["saludo", "propia"]


# ------------------------------------------------------------------ prompt
@pytest.mark.asyncio
async def test_prompt_incluye_bloque_de_flujo() -> None:
    spec = make_spec()
    ctx = PromptContext(
        assistant_name="A",
        business_description="B",
        flow_summary="1. Saludo\n   Herramientas: crear_lead",
    )
    blocks = await build_system_blocks(spec, ctx)
    assert any("## Flujo de atención" in b and "crear_lead" in b for b in blocks)
