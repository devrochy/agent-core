"""HU-AOS-03 · contrato `procedure/1`, linter y conversión de flujos (ADR-0113).

Unitarios, sin BD ni red. El catálogo de acciones sale del binario real
(`plugin_manager.scan()`, como `tests/test_provisioning_sin_llm.py`): si en el entorno
falta una habilidad, estos tests fallan. Los 4 procedimientos críticos del diseño (más
`consultar_planes`) viven en `tests/fixtures/procedures/` y también los corre el CI con
`agent-core procedures lint`.
"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from agentcore_contracts import AgentSpec, Procedure, ToolBinding, __version__
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agents import flow as flow_mod
from core.agents.flow import (
    FlowSection,
    _parse_sections,
    confirmed_actions_v2,
    flow_to_procedures,
    resolve_flow,
)
from core.kernel.procedures import (
    ActionInfo,
    Issue,
    catalogo_de_acciones,
    compilar_afirmaciones,
    lint,
    revisar,
    validar,
)
from core.orchestrator.verificador.evidencia import Evidencia
from core.orchestrator.verificador.verificador import verificar
from core.plugin_manager import plugin_manager

FIXTURES = Path(__file__).parent / "fixtures" / "procedures"
CRITICOS = ["consultar_planes", "contratar_plan", "pedido_con_entrega", "agendar_cita", "cobro_suscripcion"]
HABILIDADES = {"pack_calendar", "pack_cobranza", "pack_commerce", "pack_payments", "pack_subscriptions"}


def fixture(nombre: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{nombre}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def modulos() -> list[Any]:
    plugin_manager.scan()
    modules = plugin_manager.list_modules()
    assert HABILIDADES <= {m.id for m in modules}, "faltan habilidades instaladas en el entorno"
    return modules


@pytest.fixture(scope="module")
def catalogo(modulos) -> Mapping[str, ActionInfo]:
    return catalogo_de_acciones(modulos)


def codes(issues: list[Issue]) -> list[str]:
    return [i.code for i in issues]


# ------------------------------------------------------------------ contrato
def test_version_del_paquete_y_export() -> None:
    assert __version__ == "3.1.0"
    assert Procedure.model_fields["contract"].default == "procedure/1"


@pytest.mark.parametrize("nombre", CRITICOS)
def test_contrato_valido_los_procedimientos_criticos(nombre: str, catalogo) -> None:
    """Gherkin «Contrato válido»: datos, etapas, acciones, confirmaciones y afirmaciones."""
    assert revisar(fixture(nombre), catalogo) == []


def test_los_criticos_cubren_las_partes_del_contrato() -> None:
    procs = [validar(fixture(n))[0] for n in CRITICOS]
    assert all(p is not None for p in procs)
    assert any(s.confirmation for p in procs for s in p.stages)  # type: ignore[union-attr]
    assert any(c.kind == "amount" for p in procs for c in p.claims)  # type: ignore[union-attr]
    assert any(s.confirm for p in procs for s in p.slots.values())  # type: ignore[union-attr]


def test_extensiones_setby_interactivo_contenido_y_candidatos(catalogo) -> None:
    """Gherkin «Datos desde lo que toca o envía el cliente»: las tres extensiones se aceptan.
    El llenado sin LLM lo hace el motor (HU-AOS-05)."""
    plan, _ = validar(fixture("contratar_plan"))
    pedido, _ = validar(fixture("pedido_con_entrega"))
    agenda, _ = validar(fixture("agendar_cita"))
    cobro, _ = validar(fixture("cobro_suscripcion"))
    assert plan and pedido and agenda and cobro
    assert plan.slots["variante_id"].setBy == "interactive:plan:"
    assert pedido.slots["ubicacion"].setBy == "content:location"
    assert cobro.slots["comprobante"].setBy == "content:image"
    hora = agenda.slots["hora"]
    assert hora.setBy == "interactive:hora:" and hora.candidates is not None
    assert hora.candidates.source == "action:check_availability.slots" and hora.candidates.max == 10


def test_forma_campo_desconocido_y_formato_con_pista() -> None:
    raw = fixture("contratar_plan")
    raw["stages"][0]["acciones"] = []
    raw["slots"]["correo"]["setBy"] = "llm"
    _, issues = validar(raw)
    por_ruta = {i.path: i.message for i in issues}
    assert "Campo desconocido" in por_ruta["stages[0].acciones"]
    assert "action:<acción>" in por_ruta["slots.correo.setBy"]
    assert validar([1, 2])[1][0].message == "El procedimiento debe ser un objeto JSON."


# ------------------------------------------------------------------ linter
def test_linter_un_mensaje_por_cada_problema_del_gherkin(catalogo) -> None:
    """Gherkin «Linter»: acción inexistente, etapa inalcanzable e irreversible sin
    confirmación → tres mensajes claros."""
    con_irreversible = dict(catalogo)
    con_irreversible["crear_pedido"] = ActionInfo("crear_pedido", "pack_commerce", "irreversible")
    raw = fixture("contratar_plan")
    raw["stages"][0]["actions"].append("vender_humo")
    raw["stages"][1].pop("exitWhen")  # `datos` queda terminal: `confirmar` y `pagar` no se alcanzan
    raw["stages"][1]["next"] = None
    issues = revisar(raw, con_irreversible)
    assert sorted(codes(issues)) == [
        "accion_inexistente",
        "etapa_inalcanzable",
        "etapa_inalcanzable",
        "irreversible_sin_confirmacion",
    ]
    mensajes = " ".join(i.message for i in issues)
    assert "'vender_humo' no existe" in mensajes
    assert "'confirmar' es inalcanzable" in mensajes and "'pagar' es inalcanzable" in mensajes
    assert "'crear_pedido' es irreversible" in mensajes


def test_irreversible_con_confirmacion_pasa(catalogo) -> None:
    con_irreversible = dict(catalogo)
    con_irreversible["crear_pedido"] = ActionInfo("crear_pedido", "pack_commerce", "irreversible")
    raw = fixture("contratar_plan")
    raw["stages"][3]["confirmation"] = "interactive_button"
    assert revisar(raw, con_irreversible) == []


def test_catalogo_real_no_marca_irreversibles_todavia(catalogo) -> None:
    """El efecto de cada acción llega con el manifiesto v2 (HU-AOS-34)."""
    assert {info.side_effect for info in catalogo.values()} == {None}


def _mutar(raw: dict[str, Any], cambio: str) -> None:
    stages, slots, claims = raw["stages"], raw["slots"], raw["claims"]
    match cambio:
        case "next_desconocido":
            stages[0]["next"] = "no_existe"
        case "salida_vacia":
            stages[0]["exitWhen"] = {}
        case "slot_desconocido":
            stages[0]["exitWhen"]["slots"].append("fantasma")
        case "confirmacion_sin_dato":
            stages[0]["exitWhen"]["confirmed"] = ["variante_id"]
        case "accion_fuera_de_etapa":
            stages[1]["exitWhen"] = {"action": "informar_plan"}
        case "accion_fuera_de_etapas":
            slots["cotizacion_id"]["setBy"] = "action:aplicar_promocion"
        case "prefijo_interactivo_repetido":
            slots["correo"]["setBy"] = "interactive:plan:"
        case "tipo_incompatible_con_contenido":
            slots["correo"]["setBy"] = "content:location"
        case "resolver_ajeno":
            slots["variante_id"]["resolver"] = "pack_payments.variante"
        case "default_invalido":
            slots["periodicidad"]["default"] = "semanal"
        case "tipo_con_enum":
            slots["periodicidad"]["type"] = "integer"
        case "condicion_circular":
            slots["correo"]["requiredWhen"] = {"slot": "correo", "equals": "x"}
        case "etapa_repetida":
            stages[1]["id"] = "elegir_plan"
        case "afirmacion_de_dinero_sin_fuente":
            claims[0]["mustMatch"] = ["slot:correo"]  # lo escribe el cliente, no el servidor
        case "afirmacion_sin_fuente":
            claims[1]["mustMatch"] = []
        case "afirmacion_sin_frases":
            claims[2]["phrases"] = []
        case "afirmacion_sin_respaldo":
            claims[2]["requires"] = "slot:variante_id"  # lo llena un botón, no una acción
        case "habilidad_no_declarada":
            stages[3]["actions"].append("cob_registrar_abono")  # de pack_cobranza, sin declararlo
        case "requires_redundante":
            raw["requires"].append("pack_commerce")
        case "habilidad_desconocida":
            raw["requires"].append("pack_fantasma")
        case "confirmacion_de_accion_ajena":
            stages[0]["confirmActions"] = ["crear_pedido"]


@pytest.mark.parametrize(
    "cambio",
    [
        "next_desconocido",
        "salida_vacia",
        "slot_desconocido",
        "confirmacion_sin_dato",
        "accion_fuera_de_etapa",
        "accion_fuera_de_etapas",
        "prefijo_interactivo_repetido",
        "tipo_incompatible_con_contenido",
        "resolver_ajeno",
        "default_invalido",
        "tipo_con_enum",
        "condicion_circular",
        "etapa_repetida",
        "afirmacion_de_dinero_sin_fuente",
        "afirmacion_sin_fuente",
        "afirmacion_sin_frases",
        "afirmacion_sin_respaldo",
        "habilidad_no_declarada",
        "requires_redundante",
        "habilidad_desconocida",
        "confirmacion_de_accion_ajena",
    ],
)
def test_cada_regla_del_linter(cambio: str, catalogo) -> None:
    raw = fixture("contratar_plan")
    _mutar(raw, cambio)
    issues = revisar(raw, catalogo)
    assert cambio in codes(issues), issues
    assert all(i.message for i in issues)


def test_irreversible_confirmada_por_accion(catalogo) -> None:
    """`confirmActions` (lo que hereda un procedimiento abierto de `requiresConfirmation`)
    cuenta como confirmación de esa acción, no de las demás."""
    con_irreversible = dict(catalogo)
    for nombre in ("crear_pedido", "confirmar_pago_pedido"):
        con_irreversible[nombre] = ActionInfo(nombre, catalogo[nombre].owner, "irreversible")
    raw = fixture("contratar_plan")
    raw["stages"][3]["confirmActions"] = ["crear_pedido"]
    issues = revisar(raw, con_irreversible)
    assert [(i.code, i.path) for i in issues] == [("irreversible_sin_confirmacion", "stages[3].actions[1]")]


def test_accion_de_otra_habilidad_por_nombre_con_requires(catalogo) -> None:
    raw = fixture("contratar_plan")
    raw["stages"][3]["actions"].append("cob_registrar_abono")
    issues = revisar(raw, catalogo)
    assert {i.code for i in issues} == {"habilidad_no_declarada"}
    assert "'cob_registrar_abono' es de 'pack_cobranza'" in issues[0].message
    raw["requires"] = ["pack_cobranza"]
    assert revisar(raw, catalogo) == []


def test_tope_de_errores(catalogo) -> None:
    raw = fixture("consultar_planes")
    raw["stages"][0]["actions"] = [f"accion_{i}" for i in range(60)]
    issues = revisar(raw, catalogo)
    assert len(issues) == 51
    assert issues[-1].code == "demasiados_errores" and "Y 11 problema(s) más" in issues[-1].message  # 60 inexistentes + la fuente del monto


def test_ruta_recorta_campos_desconocidos_largos() -> None:
    raw = fixture("consultar_planes")
    raw["x" * 500] = 1
    [issue] = validar(raw)[1]
    assert issue.code == "forma" and len(issue.path) == 60 and issue.path.endswith("…")


@pytest.mark.parametrize("frase", ["  ", "ok", "   a  "])
def test_frases_demasiado_cortas_se_rechazan(frase: str) -> None:
    raw = fixture("agendar_cita")
    raw["claims"][0]["phrases"] = [frase]
    [issue] = validar(raw)[1]
    assert issue.path == "claims[0].phrases[0]" and "mínimo 3" in issue.message


@pytest.mark.parametrize("default", [{"a": 1}, [1], "x" * 101])
def test_default_es_un_escalar_acotado(default: Any) -> None:
    raw = fixture("contratar_plan")
    raw["slots"]["periodicidad"]["default"] = default
    assert validar(raw)[0] is None


def test_dinero_sin_ninguna_fuente(catalogo) -> None:
    raw = fixture("consultar_planes")
    raw["claims"] = [{"kind": "amount"}]
    issues = revisar(raw, catalogo)
    assert codes(issues) == ["afirmacion_de_dinero_sin_fuente"]
    assert "fuente del servidor" in issues[0].message


def test_candidatos_de_una_accion_inexistente(catalogo) -> None:
    raw = fixture("agendar_cita")
    raw["slots"]["hora"]["candidates"]["source"] = "action:horarios_magicos.slots"
    issues = revisar(raw, catalogo)
    assert codes(issues) == ["accion_inexistente"]
    assert issues[0].path == "slots.hora.candidates.source"


def test_salto_con_next_hace_inalcanzable_la_etapa_saltada(catalogo) -> None:
    raw = fixture("contratar_plan")
    raw["stages"][0]["next"] = "confirmar"  # `datos` queda fuera del recorrido
    issues = revisar(raw, catalogo)
    assert codes(issues) == ["etapa_inalcanzable"]
    assert "'datos'" in issues[0].message


# ------------------------------------------------------------------ afirmaciones → verificador
def test_afirmaciones_compiladas_pasan_por_el_verificador_unico() -> None:
    proc, _ = validar(fixture("contratar_plan"))
    assert proc is not None
    afirmaciones = compilar_afirmaciones(proc)
    # amount y link sin frases los cubren las comprobaciones genéricas del verificador.
    assert [(a.label, set(a.tools), a.basta_url) for a in afirmaciones] == [
        ("pack_commerce.contratar_plan:2", {"crear_pedido"}, False)
    ]
    borrador = "Listo, tu pedido creado ya está en camino."
    sin = verificar(borrador, Evidencia.desde([]), afirmaciones)
    assert not sin.passed and sin.motivo == "status_sin_evidencia"
    con = verificar(borrador, Evidencia.desde([{"_tool_name": "crear_pedido", "_ok": True}]), afirmaciones)
    assert con.passed


def test_frases_se_escapan_nunca_regex_crudo() -> None:
    raw = fixture("agendar_cita")
    raw["claims"][0]["phrases"] = ["(a+)+$ quedó.*agendada"]
    proc, _ = validar(raw)
    assert proc is not None
    [afirmacion] = compilar_afirmaciones(proc)
    assert afirmacion.patron.search("x (a+)+$ quedó.*agendada x")
    assert not afirmacion.patron.search("aaaa quedó bien agendada")


def test_link_con_frases_se_respalda_tambien_por_url() -> None:
    proc, _ = validar(fixture("cobro_suscripcion"))
    assert proc is not None
    [afirmacion] = compilar_afirmaciones(proc)
    assert afirmacion.basta_url and afirmacion.tools == frozenset({"sub_link_pago"})


# ------------------------------------------------------------------ catálogo
def test_catalogo_es_del_binario_sin_negocio(modulos, monkeypatch: pytest.MonkeyPatch) -> None:
    """El catálogo no recibe spec ni negocio y nunca trae tools declarativas: es el mismo
    para todos los negocios."""
    assert list(inspect.signature(catalogo_de_acciones).parameters) == ["modules"]

    def _prohibido(*_a, **_k):
        raise AssertionError("el catálogo no debe leer tools declarativas de un negocio")

    import core.mcp.declarative as declarative
    from core.kernel.procedures import catalogo as catalogo_mod

    monkeypatch.setattr(declarative, "tools_for_spec", _prohibido)
    monkeypatch.setattr(catalogo_mod, "_cache", None)
    catalogo = catalogo_de_acciones(modulos)
    assert catalogo_de_acciones(modulos) is catalogo  # cacheado por identidad de los módulos
    with pytest.raises(TypeError):
        catalogo["x"] = None  # type: ignore[index]
    assert catalogo["escalate_to_human"].owner == "core"
    assert catalogo["sub_link_pago"].owner == "core"
    assert catalogo["confirmar_pago_pedido"].owner == "pack_commerce"
    assert "generar_link_pago" not in catalogo  # retirada (ADR-0087): pagos no expone tools
    assert catalogo["cob_consultar_saldo"].owner == "pack_cobranza"


# ------------------------------------------------------------------ conversión
def _spec(packs: list[str]) -> AgentSpec:
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
        model={"primary": "deepseek-flash"},
        memory={"shortTermTurns": 30},
        playbooks=[],
        tools=[],
        policies={"maxToolCallsPerTurn": 10},
        packs=packs,
    )


async def test_conversion_del_flujo_compuesto_real_sin_cambiar_acciones(
    catalogo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gherkin «Conversión»: `core/flow.json` + los flows de todos los packs + una sección
    propia del negocio + un override de una sección de pack → un procedimiento abierto
    por sección, con exactamente las mismas acciones."""
    propias = [
        FlowSection(id="planes", label="Planes", purpose="Planes del negocio", pack="tenant", tools=["informar_plan"]),
        # Override real de `pack_commerce:cotizacion`: trae una tool de otra sección.
        FlowSection(
            id="cotizacion",
            label="Cotización",
            purpose="Con stock",
            pack="pack_commerce",
            tools=["generar_cotizacion", "confirmar_cotizacion", "consultar_inventario"],
        ),
    ]
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: propias)
    packs = sorted(HABILIDADES - {"pack_payments"})
    flow = await resolve_flow(_spec(packs), slug="acme", available=set(catalogo))
    assert flow is not None and flow.unknown_tools == []
    qids = {s.qualified_id for s in flow.sections}
    assert {"core:saludo", "core:escalamiento", "tenant:planes", "pack_commerce:cotizacion"} <= qids
    assert {s.pack for s in flow.sections} >= {"core", "tenant", *packs}

    procedures = flow_to_procedures(flow.sections, catalogo=catalogo)

    assert len(procedures) == len(flow.sections)
    for section, proc in zip(flow.sections, procedures, strict=True):
        assert proc.id == f"{section.pack}.{section.id}"
        assert [s.id for s in proc.stages] == [section.id]
        assert proc.stages[0].actions == section.tools
        assert proc.stages[0].exitWhen is None and proc.slots == {} and proc.claims == []
        assert proc.title == section.label and proc.stages[0].goal == section.purpose
        # Linter completo: acciones existentes y de habilidades declaradas en `requires`.
        assert lint(proc, catalogo) == [], proc.id
    cotizacion = next(p for p in procedures if p.id == "pack_commerce.cotizacion")
    assert "consultar_inventario" in cotizacion.stages[0].actions
    pedido = next(p for p in procedures if p.id == "pack_commerce.pedido")
    assert pedido.requires == []  # el link lo da `confirmar_pago_pedido`, del propio pack
    assert [a for p in procedures for a in p.stages[0].actions] == [
        t for s in flow.sections for t in s.tools
    ]


async def test_conversion_hereda_la_confirmacion_del_binding_v2(
    catalogo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0113 §5: el procedimiento abierto hereda `requiresConfirmation` del binding v2 y
    el linter la acepta como confirmación, así marcar una acción irreversible no obliga a
    cambiar la conducta al convertir."""
    monkeypatch.setattr(flow_mod, "load_tenant_flow", lambda slug: None)
    spec = _spec(["pack_commerce", "pack_payments"])
    spec.tools = [
        ToolBinding(tool="crear_pedido", enabled=True, requiresConfirmation=True),
        ToolBinding(tool="agregar_item_pedido", enabled=False, requiresConfirmation=True),
    ]
    flow = await resolve_flow(spec, slug="acme", available=set(catalogo))
    assert flow is not None
    con_irreversible = dict(catalogo)
    con_irreversible["crear_pedido"] = ActionInfo("crear_pedido", "pack_commerce", "irreversible")

    procedures = flow_to_procedures(flow.sections, catalogo=catalogo, confirmed_actions=confirmed_actions_v2(spec))
    pedido = next(p for p in procedures if p.id == "pack_commerce.pedido")
    assert pedido.stages[0].confirmActions == ["crear_pedido"]  # el binding deshabilitado no cuenta
    assert all(lint(p, con_irreversible) == [] for p in procedures)

    sin_binding = flow_to_procedures(flow.sections, catalogo=catalogo)
    pedido = next(p for p in sin_binding if p.id == "pack_commerce.pedido")
    assert codes(lint(pedido, con_irreversible)) == ["irreversible_sin_confirmacion"]


def test_conversion_es_total_con_los_bordes_del_flujo() -> None:
    """Toda sección que `_parse_sections` acepta se convierte sin error (formatos del
    contrato ⊇ formatos del flujo)."""
    raw = {
        "sections": [
            {"id": "2x1_promo", "label": "L" * 500, "purpose": "P" * 900, "pack": "tenant", "tools": ["ConsultarStock"]},
            {"id": "_vip", "pack": "Pack-Raro.v2", "tools": [f"t{i}" for i in range(60)]},
            {"id": "sin_tools", "pack": "", "tools": []},
            {"id": "x" * 40, "label": "", "purpose": "", "pack": "core", "tools": ["A" * 80]},
        ]
    }
    sections = _parse_sections(raw)
    assert len(sections) == 4
    procedures = flow_to_procedures(sections)
    assert [p.id for p in procedures] == [
        "tenant.2x1_promo",
        "pack_raro_v2._vip",
        "tenant.sin_tools",
        f"core.{'x' * 40}",
    ]
    for section, proc in zip(sections, procedures, strict=True):
        assert proc.stages[0].actions == section.tools
        assert validar(proc.model_dump())[1] == []


# ------------------------------------------------------------------ endpoint
@pytest.fixture
def client(modulos, monkeypatch: pytest.MonkeyPatch):
    from core.api import deps
    from core.api.errors import ApiError, api_error_handler
    from core.api.routers import procedures as router_mod
    from core.db.models import Admin

    acceso: dict[str, str | None] = {"plugins": "write"}

    async def fake_principal():
        admin = Admin(username="autor", email="a@x.test", password_hash="x", role="owner", active=True)
        yield deps.Principal(admin=admin, tenant_id=None, tenants=[], permissions={})

    async def fake_access(_admin, section, _tenant_id):
        return acceso.get(section)

    monkeypatch.setattr(deps, "effective_access", fake_access)
    monkeypatch.setattr(router_mod.plugin_manager, "list_modules", lambda: modulos)
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[deps.get_principal] = fake_principal
    test_client = TestClient(app)
    test_client.acceso = acceso  # type: ignore[attr-defined]
    return test_client


URL = "/api/v1/admin/procedures/validate"


def test_endpoint_valido_y_con_errores(client) -> None:
    ok = client.post(URL, json={"procedure": fixture("contratar_plan")})
    assert ok.status_code == 200 and ok.json() == {"valid": True, "errors": []}

    raw = fixture("contratar_plan")
    raw["stages"][0]["actions"].append("vender_humo")
    malo = client.post(URL, json={"procedure": raw})
    assert malo.status_code == 200
    body = malo.json()
    assert body["valid"] is False
    assert body["errors"] == [
        {
            "code": "accion_inexistente",
            "path": "stages[0].actions[2]",
            "message": "La acción 'vender_humo' no existe: ni el core ni ninguna habilidad la trae.",
        }
    ]


def test_endpoint_sin_permiso_de_escritura(client) -> None:
    client.acceso["plugins"] = "read"
    assert client.post(URL, json={"procedure": fixture("contratar_plan")}).status_code == 403
    client.acceso["plugins"] = None
    assert client.post(URL, json={"procedure": fixture("contratar_plan")}).status_code == 403


def test_endpoint_sin_sesion_es_401(modulos) -> None:
    from core.api.errors import ApiError, api_error_handler
    from core.api.routers import procedures as router_mod

    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.include_router(router_mod.router, prefix="/api/v1")
    resp = TestClient(app).post(URL, json={"procedure": {}})
    assert resp.status_code == 401


def test_endpoint_topes_y_cuerpo_invalido(client) -> None:
    grande = copy.deepcopy(fixture("contratar_plan"))
    grande["triggers"]["examples"] = ["x" * 300] * 300
    resp = client.post(URL, json={"procedure": grande})
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "payload_too_large"
    assert client.post(URL, content=b"{no es json", headers={"content-type": "application/json"}).status_code == 400
    assert client.post(URL, json={"procedimiento": {}}).status_code == 400
    anidado = b'{"procedure": ' + b"[" * 20000 + b"]" * 20000 + b"}"
    resp = client.post(URL, content=anidado, headers={"content-type": "application/json"})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_json"
    enorme = b'{"procedure": ' + b"9" * 5000 + b"}"  # pasa el tope de dígitos de int()
    resp = client.post(URL, content=enorme, headers={"content-type": "application/json"})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_json"
    assert client.post(URL, json={"procedure": fixture("contratar_plan"), "extra": 1}).status_code == 400
    forma = client.post(URL, json={"procedure": []}).json()
    assert forma["valid"] is False and forma["errors"][0]["code"] == "forma"
