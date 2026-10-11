"""HU-AOS-12 · lector tolerante AgentSpec v2 → agentspec/3 + kernelspec/1 (ADR-0113 §5).

Unitarios, sin BD ni red. El catálogo sale del binario real y el flujo, de los
`flow.json` reales de los packs (más una sección `tenant` que nombra una declarativa).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import pytest
from agentcore_contracts import AgentSpec, SecretRef

from core.agents.contrato_v3 import revisar_spec
from core.agents.flow import FlowSection, compose_flow, load_core_flow, load_pack_flows
from core.agents.tolerante import (
    KEY_GENERAL,
    acciones_ofrecidas,
    candidatas_del_spec,
    convertir_v2,
)
from core.kernel.procedures import ActionInfo, catalogo_de_acciones, revisar
from core.plugin_manager import plugin_manager

PACKS = ["pack_commerce", "pack_payments"]
SECRETO = "Bearer s3cr3t0-que-no-se-filtra"


@pytest.fixture(scope="module")
def catalogo() -> Mapping[str, ActionInfo]:
    return catalogo_de_acciones(plugin_manager.scan())


def v2_completo() -> dict[str, Any]:
    """Un AgentSpec v2 con TODOS los campos con valores no default."""
    return {
        "id": "agent-solvershub",
        "version": "1.0.7",
        "tenantId": "11111111-1111-1111-1111-111111111111",
        "persona": {
            "role": "Asesora de SolversHub",
            "tone": "informal",
            "warmth": 4,
            "emojis": "free",
            "maxLength": 700,
            "forbiddenPhrases": ["como modelo de lenguaje"],
        },
        "model": {"primary": "deepseek-flash", "fallback": "gpt-4o-mini", "router": "difficulty", "temperature": 0.5},
        "memory": {"shortTermTurns": 40, "rag": {"topK": 5, "minScore": 0.55}},
        "playbooks": [
            {"name": "identidad", "description": "Identidad"},
            {"name": "ventas", "description": "Ventas"},
        ],
        "tools": [
            {"tool": "crear_pedido", "enabled": True, "requiresConfirmation": True, "maxCallsPerConversation": 3},
            {"tool": "send_email", "enabled": False, "scopes": ["correo"]},
        ],
        "policies": {"maxToolCallsPerTurn": 7, "budgetUsdPerConversation": 0.8},
        "packs": PACKS,
        "extensions": {
            "guardrails": [
                {"kind": "forbidden_phrase", "phrases": ["gratis para siempre"], "action": "escalate"},
                {"kind": "required_tool", "phrases": ["link de pago"], "tool": "confirmar_pago_pedido"},
                {"kind": "max_length", "value": 900},
            ],
            "tools": [
                {
                    "name": "consultar_envio",
                    "description": "Estado de una guía",
                    "url": "https://api.transportadora.co/v1/guias/{guia}",
                    "allowed_domains": ["api.transportadora.co"],
                    "headers": {"Authorization": SECRETO, "X-Cliente": "solvershub"},
                    "parameters": {"type": "object", "properties": {"guia": {"type": "string"}}},
                }
            ],
            "nota_interna": {"de": "migración"},
        },
    }


def secciones(spec: AgentSpec, candidatas: list[str]) -> list[FlowSection]:
    """El flujo compuesto como lo resuelve `resolve_flow` (sin disco del negocio)."""
    propia = FlowSection(id="envios", label="Envíos", purpose="Rastrear guías", pack="tenant", tools=["consultar_envio"])
    compuesto = compose_flow(load_core_flow(), load_pack_flows(sorted(spec.packs)), [propia])
    disponibles = set(candidatas)
    resueltas = []
    for s in compuesto:
        tools = [t for t in s.tools if t in disponibles]
        if tools:
            resueltas.append(FlowSection(s.id, s.label, s.purpose, s.pack, tools))
    return resueltas


def convertir(raw: dict[str, Any], catalogo, *, con_flujo: bool = True):  # type: ignore[no-untyped-def]
    spec = AgentSpec.model_validate(raw)
    candidatas, siempre = candidatas_del_spec(spec, catalogo)
    return convertir_v2(
        spec,
        catalogo=catalogo,
        secciones=secciones(spec, candidatas) if con_flujo else None,
        candidatas=candidatas,
        siempre=siempre,
    ), candidatas, siempre


# ------------------------------------------------------------------ campo por campo
def test_campo_por_campo_segun_adr_0113(catalogo) -> None:
    """Gherkin «Campo por campo»: cada campo v2 queda en su lugar de v3."""
    c, _, _ = convertir(v2_completo(), catalogo)
    g, k = c.general, c.kernel
    # id, version, tenantId → identidad de la fila.
    assert (c.identidad.id, c.identidad.version, c.identidad.tenant_id) == (
        "agent-solvershub",
        "1.0.7",
        "11111111-1111-1111-1111-111111111111",
    )
    # persona.role/tone/warmth/emojis → voz de la conexión (KernelSpec).
    assert k.voice.model_dump() == {"role": "Asesora de SolversHub", "tone": "informal", "warmth": 4, "emojis": "free"}
    # maxLength y forbiddenPhrases → persona del agente, que hereda la voz.
    assert g.persona.model_dump() == {
        "inheritConnectionVoice": True,
        "maxLength": 700,
        "forbiddenPhrases": ["como modelo de lenguaje"],
    }
    assert g.model.model_dump() == {"primary": "deepseek-flash", "fallback": "gpt-4o-mini", "router": "difficulty", "temperature": 0.5}
    assert g.memory.shortTermTurns == 40 and g.memory.rag is not None
    assert (g.memory.rag.topK, g.memory.rag.minScore) == (5, 0.55)
    assert g.memory.readScopes == ["customer", "connection", "tenant"]
    assert [p.name for p in g.playbooks] == ["identidad", "ventas"]
    # policies: maxToolCallsPerTurn en el agente; budgetUsdPerConversation en el KernelSpec.
    assert g.policies.maxToolCallsPerTurn == 7
    assert k.policies.budgetUsdPerConversation == 0.8
    assert k.routing.defaultAgent == KEY_GENERAL
    # extensions.guardrails y extensions.tools → primer nivel; el resto sigue reservado.
    assert [r.kind for r in g.guardrails] == ["forbidden_phrase", "required_tool", "max_length"]
    assert g.guardrails[0].action == "escalate" and g.guardrails[2].value == 900
    assert [t.name for t in g.declarativeTools] == ["consultar_envio"]
    assert g.declarativeTools[0].allowedDomains == ["api.transportadora.co"]
    assert g.extensions == {"nota_interna": {"de": "migración"}}
    # Bindings v2 tal cual (incluido el deshabilitado).
    por_tool = {b.tool: b for b in g.tools}
    assert por_tool["crear_pedido"].requiresConfirmation and por_tool["crear_pedido"].maxCallsPerConversation == 3
    assert not por_tool["send_email"].enabled and por_tool["send_email"].scopes == ["correo"]
    assert c.avisos == ["extensions.nota_interna: se conserva en extensions (reservado, sin semántica)"]


def test_el_negocio_de_la_fila_gana_sobre_el_tenant_id_del_json(catalogo) -> None:
    spec = AgentSpec.model_validate(v2_completo())
    candidatas, siempre = candidatas_del_spec(spec, catalogo)
    c = convertir_v2(
        spec, tenant_id="22222222-2222-2222-2222-222222222222", catalogo=catalogo,
        secciones=None, candidatas=candidatas, siempre=siempre,
    )
    assert c.identidad.tenant_id == "22222222-2222-2222-2222-222222222222"
    assert any("distinto del negocio de la fila" in a for a in c.avisos)


def test_header_con_pinta_de_credencial_tambien_pasa_a_secreto(catalogo) -> None:
    """Decisión del dueño (2026-10-11): fuera de la lista del contrato, un header con nombre de
    credencial también pasa a secreto; los demás quedan literales."""
    raw = v2_completo()
    raw["extensions"]["tools"][0]["headers"]["X-Client-Secret"] = "tok-literal-123"
    raw["extensions"]["tools"][0]["headers"]["X-Auth-Token"] = "tok-movido-456"
    raw["extensions"]["tools"][0]["headers"]["X-Request-Id"] = "fijo"
    c, _, _ = convertir(raw, catalogo)
    assert c.secretos["consultar_envio_x_auth_token"].get_secret_value() == "tok-movido-456"
    assert c.secretos["consultar_envio_x_client_secret"].get_secret_value() == "tok-literal-123"
    headers = c.general.declarativeTools[0].headers
    assert headers["X-Client-Secret"] == SecretRef(secret="consultar_envio_x_client_secret")
    assert headers["X-Request-Id"] == "fijo" and headers["X-Cliente"] == "solvershub"
    assert all("tok-" not in a for a in c.avisos)
    assert revisar_spec(c.general.model_dump(mode="json", by_alias=True), catalogo) == []


def test_credenciales_de_la_url_y_el_body_pasan_a_secretos(catalogo) -> None:
    raw = v2_completo()
    tool = raw["extensions"]["tools"][0]
    tool["url"] = "https://user:p%40ss@api.transportadora.co/v1/guias/{guia}?api_key=k%2B1&formato=json&q={q}"
    tool["headers"] = {"X-Cliente": "solvershub"}
    tool["method"] = "POST"
    tool["body"] = {"guia": "{guia}", "auth": {"usuario": "u", "password": "clave-body"}, "access_token": "tok-body"}
    c, _, _ = convertir(raw, catalogo)
    t = c.general.declarativeTools[0]
    assert t.url == (
        "https://api.transportadora.co/v1/guias/{guia}?api_key={secret:consultar_envio_api_key}&formato=json&q={q}"
    )
    assert t.headers["Authorization"] == SecretRef(secret="consultar_envio_authorization")
    assert t.body == {
        "guia": "{guia}",
        "auth": {"usuario": "u", "password": {"secret": "consultar_envio_auth_password"}},
        "access_token": {"secret": "consultar_envio_access_token"},
    }
    valores = {k: v.get_secret_value() for k, v in c.secretos.items()}
    assert valores == {
        "consultar_envio_api_key": "k%2B1",  # tal como estaba escrito en la query
        "consultar_envio_auth_password": "clave-body",
        "consultar_envio_access_token": "tok-body",
        # Lo que httpx manda por el userinfo `user:p@ss`.
        "consultar_envio_authorization": "Basic dXNlcjpwQHNz",
    }
    dump = c.general.model_dump_json()
    assert "clave-body" not in dump and "tok-body" not in dump and "k%2B1" not in dump and "p%40ss" not in dump
    assert revisar_spec(c.general.model_dump(mode="json", by_alias=True), catalogo) == []


def test_credenciales_de_url_y_body_sin_dominios_se_descartan(catalogo) -> None:
    raw = v2_completo()
    tool = raw["extensions"]["tools"][0]
    tool["allowed_domains"] = []
    tool["url"] = "https://u:p@api.transportadora.co/x?token=abc&q={q}"
    tool["body"] = {"secret_key": "zzz", "n": 1}
    c, _, _ = convertir(raw, catalogo)
    t = c.general.declarativeTools[0]
    assert c.secretos == {}
    assert t.url == "https://api.transportadora.co/x?token=&q={q}"
    assert t.body == {"n": 1}
    assert "zzz" not in c.general.model_dump_json()


def test_ningun_campo_v2_queda_sin_destino() -> None:
    """Si el v2 gana un campo, este test obliga a decidir su lugar en v3 (ADR-0113)."""
    destinos = {
        "id": "identidad",
        "version": "identidad",
        "tenantId": "identidad",
        "persona": "kernel.voice + general.persona",
        "model": "general.model",
        "memory": "general.memory",
        "playbooks": "general.playbooks",
        "tools": "general.tools",
        "policies": "general.policies + kernel.policies",
        "packs": "pack_installations (ADR-0118), vía enabled_packs",
        "extensions": "general.guardrails + general.declarativeTools + general.extensions",
    }
    assert set(AgentSpec.model_fields) == set(destinos)


def test_la_salida_pasa_el_validador(catalogo) -> None:
    c, _, _ = convertir(v2_completo(), catalogo)
    assert revisar_spec(c.general.model_dump(mode="json", by_alias=True), catalogo, "agentspec/3") == []
    assert revisar_spec(c.kernel.model_dump(mode="json", by_alias=True), catalogo, "kernelspec/1") == []


# ------------------------------------------------------------------ acciones y procedimientos
def test_tools_tiene_las_acciones_que_hoy_ofrece_el_loop(catalogo) -> None:
    """Gherkin «Lector tolerante»: flujo v2 + always_available + declarativas de secciones tenant."""
    c, candidatas, siempre = convertir(v2_completo(), catalogo)
    spec = AgentSpec.model_validate(v2_completo())
    flujo = secciones(spec, candidatas)
    habilitadas = {b.tool for b in c.general.tools if b.enabled}
    en_flujo = {t for s in flujo for t in s.tools}
    assert {"consultar_inventario", "generar_cotizacion", "confirmar_pago_pedido"} <= en_flujo
    assert (en_flujo - {"send_email"}) <= habilitadas
    assert (siempre - {"send_email"}) <= habilitadas  # always_available (core)
    assert "consultar_envio" in habilitadas  # declarativa nombrada por la sección tenant
    # Lo único deshabilitado es lo que el v2 deshabilitó.
    assert {b.tool for b in c.general.tools if not b.enabled} == {"send_email"}
    # Nada fuera de lo que el loop ofrece (ni acciones de packs no habilitados).
    assert {b.tool for b in c.general.tools} <= set(acciones_ofrecidas(flujo, candidatas, siempre)) | {"send_email"}
    assert not any(catalogo[b.tool].owner == "pack_calendar" for b in c.general.tools if b.tool in catalogo)


def test_procedimientos_abiertos_por_seccion(catalogo) -> None:
    c, candidatas, _ = convertir(v2_completo(), catalogo)
    spec = AgentSpec.model_validate(v2_completo())
    flujo = secciones(spec, candidatas)
    assert [p.id for p in c.procedimientos] == [f"{s.pack}.{s.id}" for s in flujo]
    assert [(r.id, r.version) for r in c.general.procedures] == [(p.id, "=0.0.0") for p in c.procedimientos]
    pedido = next(p for p in c.procedimientos if p.id == "pack_commerce.pedido")
    # El requiresConfirmation del binding v2 pasa a la etapa del procedimiento abierto.
    assert pedido.stages[0].confirmActions == ["crear_pedido"]
    # Los de packs pasan el linter de procedure/1 contra el binario. La sección tenant usa
    # una declarativa, que el catálogo del binario no trae (la valida el build, HU-AOS-14).
    for proc in c.procedimientos:
        if proc.owner != "tenant":
            assert revisar(proc.model_dump(mode="json"), catalogo) == [], proc.id


def test_sin_flujo_todas_las_candidatas(catalogo) -> None:
    c, candidatas, _ = convertir(v2_completo(), catalogo, con_flujo=False)
    assert c.procedimientos == [] and c.general.procedures == []
    assert set(candidatas) <= {b.tool for b in c.general.tools}


def test_binding_nuevo_concede_los_scopes_que_exige_la_accion() -> None:
    """Hoy una acción sin binding corre (fail-open, ADR-0019): el binding nuevo no la frena."""
    catalogo = {
        "accion_con_scope": ActionInfo(name="accion_con_scope", owner="core", scopes=("crm:lead:w",)),
    }
    spec = AgentSpec.model_validate(v2_completo() | {"tools": [], "extensions": {}})
    c = convertir_v2(spec, catalogo=catalogo, secciones=None, candidatas=["accion_con_scope"], siempre=set())
    assert [(b.tool, b.scopes) for b in c.general.tools] == [("accion_con_scope", ["crm:lead:w"])]


def test_candidatas_core_siempre_y_packs_habilitados(catalogo) -> None:
    from core.subscriptions.tools import SubscriptionSkill
    from core.tools.core_skill import CoreSkill

    spec = AgentSpec.model_validate(v2_completo())
    candidatas, siempre = candidatas_del_spec(spec, catalogo)
    core = {t.name for skill in (CoreSkill(), SubscriptionSkill()) for t in skill.get_tools() if t.always_available}
    assert siempre == core
    assert core <= set(candidatas)
    assert "consultar_envio" in candidatas
    # pack_payments está habilitado pero no aporta tools (adenda 2026-10-10 de ADR-0087).
    assert {catalogo[t].owner for t in candidatas if t in catalogo} == {"core", "pack_commerce"}


# ------------------------------------------------------------------ secretos
def test_secretos_literales_pasan_a_referencia(catalogo) -> None:
    """Gherkin «Tools declarativas sin secretos» (lado del lector)."""
    c, _, _ = convertir(v2_completo(), catalogo)
    headers = c.general.declarativeTools[0].headers
    assert headers == {"Authorization": SecretRef(secret="consultar_envio_authorization"), "X-Cliente": "solvershub"}
    assert c.secretos["consultar_envio_authorization"].get_secret_value() == SECRETO
    # Nada del valor en la salida: ni en el repr, ni en el dump, ni en los avisos.
    assert SECRETO not in repr(c)
    assert SECRETO not in c.general.model_dump_json()
    assert all(SECRETO not in a for a in c.avisos)


def test_nombres_de_secreto_unicos(catalogo) -> None:
    raw = v2_completo()
    raw["extensions"]["tools"][0]["headers"] = {"Authorization": "a", "Cookie": "b", "x-api-key": "c"}
    raw["extensions"]["tools"].append(
        {"name": "consultar_envio_v2", "url": "https://b.co", "allowed_domains": ["b.co"], "headers": {"AUTHORIZATION": "d"}}
    )
    c, _, _ = convertir(raw, catalogo)
    assert sorted(c.secretos) == [
        "consultar_envio_authorization",
        "consultar_envio_cookie",
        "consultar_envio_v2_authorization",
        "consultar_envio_x_api_key",
    ]


# ------------------------------------------------------------------ v2 "sucios"
def _sucio(catalogo) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    raw = v2_completo()
    raw["persona"]["maxLength"] = 0  # 0 = sin tope (lo permite el panel)
    raw["persona"]["role"] = "   "
    raw["memory"] = {"shortTermTurns": 9999, "rag": {"topK": 500, "minScore": 3}}
    raw["policies"] = {"maxToolCallsPerTurn": 500, "budgetUsdPerConversation": None}
    raw["playbooks"].append({"name": "../fuera", "description": "x"})
    raw["tools"] += [
        {"tool": "retirada_v1", "enabled": True},
        {"tool": "crear_pedido", "enabled": True, "maxCallsPerConversation": 1, "scopes": []},
        {"tool": "consultar_inventario", "enabled": True, "maxCallsPerConversation": 0},
    ]
    raw["extensions"]["guardrails"] += [
        {"kind": "required_tool", "phrases": ["agendé"], "tool": "tool-que-no-existe"},
        {"kind": "inventado"},
    ]
    raw["extensions"]["tools"] += [
        {"name": "crear_pedido", "url": "https://x.co", "allowed_domains": ["x.co"]},
        {"name": "sin url"},
        {"name": "interna", "url": "https://x.co", "allowed_domains": ["redis", "10.0.0.1", "x.co"]},
        {"name": "consultar_envio", "url": "https://dup.co"},
    ]
    return raw


def test_v2_sucio_siempre_pasa_el_validador(catalogo) -> None:
    """La salida del lector nunca bloquea el backfill: lo muerto se poda con aviso."""
    c, _, _ = convertir(_sucio(catalogo), catalogo)
    assert revisar_spec(c.general.model_dump(mode="json", by_alias=True), catalogo) == []
    assert revisar_spec(c.kernel.model_dump(mode="json", by_alias=True), catalogo) == []
    g = c.general
    por_tool = {b.tool: b for b in g.tools}
    assert "retirada_v1" not in por_tool
    # Repetidos: el loop aplica todos los bindings; se fusionan al más restrictivo.
    assert por_tool["crear_pedido"].maxCallsPerConversation == 1
    # maxCallsPerConversation 0: hoy se deniega desde la primera llamada.
    assert not por_tool["consultar_inventario"].enabled
    assert [r.kind for r in g.guardrails][-1] == "forbidden_phrase"
    assert g.guardrails[-1].phrases == ["agendé"]
    assert [t.name for t in g.declarativeTools] == ["consultar_envio", "interna"]
    assert g.declarativeTools[1].allowedDomains == ["x.co"]
    assert g.persona.maxLength == 0
    assert (g.memory.shortTermTurns, g.policies.maxToolCallsPerTurn) == (500, 100)
    assert c.kernel.voice.role == "asistente" and c.kernel.policies.budgetUsdPerConversation is None
    assert len(c.avisos) >= 8
    assert all(SECRETO not in a for a in c.avisos)


def test_default_spec_del_alta(catalogo) -> None:
    from core.agents.spec import DEFAULT_SPEC

    raw = copy.deepcopy(DEFAULT_SPEC) | {"tenantId": "t"}
    c, _, _ = convertir(raw, catalogo, con_flujo=False)
    assert revisar_spec(c.general.model_dump(mode="json", by_alias=True), catalogo) == []
    assert c.avisos == []


def test_credencial_por_http_pasa_a_https(catalogo) -> None:
    raw = v2_completo()
    raw["extensions"]["tools"][0]["url"] = "http://api.transportadora.co/v1/guias/{guia}"
    c, _, _ = convertir(raw, catalogo)
    assert c.general.declarativeTools[0].url == "https://api.transportadora.co/v1/guias/{guia}"
    assert any("pasa a https" in a for a in c.avisos)


def test_header_con_salto_de_linea_se_descarta(catalogo) -> None:
    raw = v2_completo()
    raw["extensions"]["tools"][0]["headers"] = {"Authorization\n": "x", "X-Cliente": "a\r\nX-Inyectado: 1"}
    c, _, _ = convertir(raw, catalogo)
    assert c.general.declarativeTools[0].headers == {}
    assert c.secretos == {}


def test_referencias_ya_escritas_por_el_editor_se_conservan(catalogo) -> None:
    raw = v2_completo()
    raw["extensions"]["tools"][0]["headers"] = {
        "Authorization": {"secret": "consultar_envio_authorization"},
        "Cookie": "sid=literal",
    }
    c, _, _ = convertir(raw, catalogo)
    headers = c.general.declarativeTools[0].headers
    assert headers["Authorization"] == SecretRef(secret="consultar_envio_authorization")
    # El literal nuevo no pisa el nombre de la referencia existente.
    assert headers["Cookie"] == SecretRef(secret="consultar_envio_cookie")
    assert list(c.secretos) == ["consultar_envio_cookie"]


def test_tool_sin_dominios_pierde_el_header_sensible_sin_extraer_secreto(catalogo) -> None:
    """HU-AOS-13: sin `allowed_domains` la tool hoy no llama a nadie (ScopedHttp por tool): el
    literal no se extrae (no tendría a dónde viajar) ni entra a la versión. Con aviso, sin valor."""
    raw = v2_completo()
    raw["extensions"]["tools"][0]["allowed_domains"] = []
    c, _, _ = convertir(raw, catalogo)
    assert c.general.declarativeTools[0].headers == {"X-Cliente": "solvershub"}
    assert c.secretos == {}
    assert any("se descarta" in a and "allowed_domains" in a for a in c.avisos)
    assert all(SECRETO not in a for a in c.avisos)
    assert SECRETO not in c.general.model_dump_json()
