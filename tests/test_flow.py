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
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=[],
        tools=[],
        policies={"maxToolCallsPerTurn": 10},
        packs=packs or [],
    )


def section(sid: str, tools: list[str], pack: str = "pack_commerce") -> FlowSection:
    return FlowSection(id=sid, label=sid.title(), purpose=f"propósito {sid}", pack=pack, tools=tools)


# ------------------------------------------------------------------ flujos reales
def test_pack_commerce_flow_existe_y_es_valido() -> None:
    sections = load_pack_flows(["pack_commerce"])
    assert [s.id for s in sections] == ["descubrimiento", "cotizacion", "pedido"]
    assert {s.pack for s in sections} == {"pack_commerce"}
    tools = {t for s in sections for t in s.tools}
    assert {"consultar_inventario", "generar_cotizacion", "crear_pedido"} <= tools


def test_core_flow_declara_saludo_y_escalamiento() -> None:
    core = flow_mod.load_core_flow()
    assert [s.qualified_id for s in core] == ["core:saludo", "core:escalamiento"]
    assert "escalate_to_human" in core[1].tools


def test_pack_no_puede_declarar_secciones_del_core(tmp_path: Path, monkeypatch) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "flow.json").write_text(
        json.dumps({"sections": [
            {"id": "saludo", "tools": ["x"]},
            {"id": "escalamiento", "pack": "core", "tools": ["escalate_to_human"]},
            {"id": "propia_del_pack", "pack": "otro", "tools": ["y"]},
        ]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(flow_mod, "pack_flow_path", lambda pid: pack_dir / "flow.json")
    sections = load_pack_flows(["pack_x"])
    assert [s.qualified_id for s in sections] == ["pack_x:propia_del_pack"]


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


# ------------------------------------------------------------------ composición (HU-FWK-01)
TRES_PACKS = ["pack_calendar", "pack_cobranza", "pack_commerce"]


def _tools_de_packs(packs: list[str]) -> set[str]:
    return {t for s in load_pack_flows(packs) for t in s.tools}


@pytest.mark.asyncio
async def test_sin_tools_ocultas_con_tres_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gherkin «Sin tools ocultas»: commerce + calendar + cobranza → todas sus tools y
    las del core (memoria, escalamiento) están en el flujo."""
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: None)
    flow = await resolve_flow(make_spec(TRES_PACKS), slug="acme")
    assert flow is not None
    nombres = set(flow.tool_names)
    assert _tools_de_packs(TRES_PACKS) <= nombres
    assert {"escalate_to_human", "update_customer_profile", "remember_fact"} <= nombres
    # Antes, `saludo` de cobranza tapaba el de commerce y se perdían estas:
    assert {"crear_lead", "consultar_inventario", "calificar_lead"} <= nombres


@pytest.mark.asyncio
async def test_sin_choques_una_sola_seccion_de_saludo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gherkin «Sin choques»: commerce y cobranza traían «saludo»; queda `core:saludo`."""
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: None)
    flow = await resolve_flow(make_spec(["pack_cobranza", "pack_commerce"]), slug="acme")
    assert flow is not None
    saludos = [s.qualified_id for s in flow.sections if s.id == "saludo"]
    assert saludos == ["core:saludo"]
    assert [s.qualified_id for s in flow.sections if s.id == "escalamiento"] == ["core:escalamiento"]
    assert flow.sections[0].qualified_id == "core:saludo"
    assert flow.sections[-1].qualified_id == "core:escalamiento"


@pytest.mark.asyncio
@pytest.mark.parametrize("packs", [["pack_commerce"], ["pack_calendar", "pack_commerce"], TRES_PACKS])
async def test_composicion_con_1_2_y_3_packs(monkeypatch: pytest.MonkeyPatch, packs) -> None:
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: None)
    flow = await resolve_flow(make_spec(packs), slug="acme")
    assert flow is not None
    qids = [s.qualified_id for s in flow.sections]
    assert len(qids) == len(set(qids))
    esperadas = {s.qualified_id for s in load_pack_flows(packs)}
    assert esperadas <= set(qids)
    assert {s.pack for s in flow.sections} == {"core", *packs}


@pytest.mark.asyncio
async def test_seccion_propia_se_suma_y_no_reemplaza_a_los_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        flow_mod, "load_tenant_flow", lambda slug: [section("planes", ["informar_plan"], pack="tenant")]
    )
    flow = await resolve_flow(make_spec(["pack_commerce"]), slug="acme")
    assert flow is not None
    assert flow.source == "tenant"
    qids = [s.qualified_id for s in flow.sections]
    assert "tenant:planes" in qids and "pack_commerce:pedido" in qids
    assert qids.index("tenant:planes") < qids.index("core:escalamiento")


@pytest.mark.asyncio
async def test_override_explicito_de_una_seccion_de_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    propia = section("pedido", ["crear_pedido", "informar_plan"], pack="pack_commerce")
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: [propia])
    flow = await resolve_flow(make_spec(["pack_commerce"]), slug="acme")
    assert flow is not None
    pedido = next(s for s in flow.sections if s.qualified_id == "pack_commerce:pedido")
    assert pedido.tools == ["crear_pedido", "informar_plan"]


@pytest.mark.asyncio
async def test_override_de_un_pack_deshabilitado_se_descarta(monkeypatch: pytest.MonkeyPatch) -> None:
    ajena = section("consulta", ["cob_consultar_saldo"], pack="pack_cobranza")
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: [ajena])
    flow = await resolve_flow(make_spec(["pack_commerce"]), slug="acme")
    assert flow is not None
    assert "cob_consultar_saldo" not in flow.tool_names


@pytest.mark.asyncio
async def test_resolve_flow_filtra_tools_no_disponibles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: None)
    monkeypatch.setattr(flow_mod, "load_core_flow", lambda: [])
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
    monkeypatch.setattr(flow_mod, "load_core_flow", lambda: [])
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


def test_tools_del_core_siempre_disponibles() -> None:
    from core.subscriptions.tools import SubscriptionSkill
    from core.tools.core_skill import CoreSkill

    assert all(t.always_available for t in CoreSkill().get_tools())
    assert all(t.always_available for t in SubscriptionSkill().get_tools())


# ------------------------------------------------------------------ siembra y migración
def _escribir(path: Path, sections: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "sections": sections}), encoding="utf-8")


def _copia(pack: str, sid: str) -> dict:
    """Copia literal de una sección del pack, como las sembraba el alta antes de ADR-0109."""
    import json as _json

    data = _json.loads((flow_mod.pack_flow_path(pack) or Path()).read_text(encoding="utf-8"))
    sec = next(s for s in data["sections"] if s["id"] == sid)
    return {**sec, "pack": pack}


def test_seccion_propia_protegida_y_reseed_sin_duplicados(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gherkin «Secciones propias protegidas» y «Re-seed sin duplicados»."""
    target = tmp_path / "acme" / "flow.json"
    monkeypatch.setattr(flow_mod, "tenant_flow_path", lambda slug: target)
    propia = {"id": "propia", "label": "Propia", "purpose": "mía", "pack": "tenant", "tools": ["mi_tool"]}
    _escribir(target, [_copia("pack_commerce", "pedido"), propia])

    flow_mod.seed_tenant_flow("acme", ["pack_commerce"])
    primera = json.loads(target.read_text(encoding="utf-8"))
    flow_mod.seed_tenant_flow("acme", ["pack_commerce"])
    segunda = json.loads(target.read_text(encoding="utf-8"))

    assert primera == segunda
    assert primera["sections"] == [propia]


def test_seed_sin_archivo_no_crea_copias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "acme" / "flow.json"
    monkeypatch.setattr(flow_mod, "tenant_flow_path", lambda slug: target)
    pasos = flow_mod.seed_tenant_flow("acme", ["pack_commerce"])
    assert pasos and not target.exists()


def test_migracion_del_flujo_de_solvershub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Gherkin «Migración»: el flujo real de `solvershub` en test (2026-10-08) tenía
    copias de packs, una sección propia marcada `pack_commerce` y `sub_gestion` con
    los nombres de antes de HU-SUB-01."""
    target = tmp_path / "solvershub" / "flow.json"
    monkeypatch.setattr(flow_mod, "tenant_flow_path", lambda slug: target)
    saludo_viejo = {**_copia("pack_commerce", "descubrimiento"), "id": "saludo"}
    saludo_viejo["tools"] = [t for t in saludo_viejo["tools"] if t != "informar_plan"]
    planes = {
        "id": "planes", "label": "Planes", "purpose": "Presentar los planes de SolversHub.",
        "pack": "pack_commerce", "tools": ["consultar_inventario", "recomendar_producto", "informar_plan"],
    }
    sub_vieja = {**_copia("pack_subscriptions", "sub_gestion"), "tools": [
        "sub_consultar_suscripcion", "sub_pausar_suscripcion", "sub_link_pago",
    ]}
    escalamiento = {"id": "escalamiento", "label": "Escalamiento", "purpose": "", "pack": "core",
                    "tools": ["escalate_to_human"]}
    _escribir(target, [
        saludo_viejo, planes, _copia("pack_commerce", "cotizacion"), _copia("pack_commerce", "pedido"),
        sub_vieja, _copia("pack_calendar", "agendamiento"), escalamiento,
    ])

    plan = flow_mod.normalize_tenant_flow("solvershub", apply=False)
    assert plan.cambia
    assert plan.pasan_a_tenant == ["pack_commerce:planes → tenant:planes"]
    assert set(plan.copias_eliminadas) == {
        "pack_commerce:saludo", "pack_commerce:cotizacion", "pack_commerce:pedido",
        "pack_subscriptions:sub_gestion", "pack_calendar:agendamiento", "core:escalamiento",
    }
    assert plan.tools_desconocidas == {
        "pack_subscriptions:sub_gestion": ["sub_consultar_suscripcion", "sub_pausar_suscripcion"]
    }
    assert plan.overrides == []
    # El dry-run no escribe.
    assert len(json.loads(target.read_text(encoding="utf-8"))["sections"]) == 7

    flow_mod.normalize_tenant_flow("solvershub", apply=True)
    final = json.loads(target.read_text(encoding="utf-8"))["sections"]
    assert [(s["pack"], s["id"]) for s in final] == [("tenant", "planes")]
    assert final[0]["tools"] == planes["tools"]
    # Idempotente.
    assert not flow_mod.normalize_tenant_flow("solvershub", apply=True).cambia


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


@pytest.mark.asyncio
async def test_copia_vieja_sin_migrar_no_tapa_al_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hasta que se migre, una copia vieja de una sección (como `sub_gestion` con los
    nombres de antes de HU-SUB-01) no reemplaza a la del pack en runtime."""
    vieja = section("sub_gestion", ["sub_consultar_suscripcion", "sub_link_pago"], pack="pack_subscriptions")
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: [vieja])
    flow = await resolve_flow(make_spec(["pack_subscriptions"]), slug="acme")
    assert flow is not None
    sub = next(s for s in flow.sections if s.qualified_id == "pack_subscriptions:sub_gestion")
    assert "sub_consultar" in sub.tools and "sub_consultar_suscripcion" not in sub.tools
