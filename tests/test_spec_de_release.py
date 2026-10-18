"""HU-AOS-15 · la vista v2 de la release (`core/agents/spec_de_release.py`) y el agente en foco.

Unitarios, sin BD ni red. Ida y vuelta: un AgentSpec v2 completo pasa por el lector tolerante
(v2 → agentspec/3 + kernelspec/1, ADR-0113 §5) y la vista lo devuelve como lo lee el loop:
campo por campo igual en todo lo que el loop usa. Más las reglas de `elegir_foco`, la omisión
fail-closed de las declarativas con referencias en la URL o el body, y la ranura del turno.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from agentcore_contracts import AgentSpec

from core.agents.flow import FlowSection, compose_flow, load_core_flow, load_pack_flows
from core.agents.spec_de_release import (
    VistaInvalida,
    declarativas_omitidas,
    spec_del_turno,
    spec_v2_desde_release,
)
from core.agents.tolerante import KEY_COORDINADOR, KEY_GENERAL, candidatas_del_spec, convertir_v2
from core.kernel.procedures import ActionInfo, catalogo_de_acciones
from core.kernel.release_del_turno import con_release
from core.plugin_manager import plugin_manager
from core.releases.cargar import ErrorDeRelease, elegir_foco
from core.shared import release_vigente
from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease

PACKS = ["pack_commerce", "pack_payments"]
T = uuid.UUID("11111111-1111-1111-1111-111111111111")
C = uuid.uuid4()


@pytest.fixture(scope="module")
def catalogo() -> Mapping[str, ActionInfo]:
    return catalogo_de_acciones(plugin_manager.scan())


def v2_completo() -> dict[str, Any]:
    return {
        "id": "agent-solvershub",
        "version": "1.0.7",
        "tenantId": str(T),
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
                {"name": "gratis", "kind": "forbidden_phrase", "phrases": ["gratis para siempre"], "action": "escalate"},
                {"name": "link", "kind": "required_tool", "phrases": ["link de pago"], "tool": "confirmar_pago_pedido"},
                {"name": "largo", "kind": "max_length", "value": 900},
            ],
            "tools": [
                {
                    "name": "consultar_envio",
                    "description": "Estado de una guía",
                    "url": "https://api.transportadora.co/v1/guias/{guia}",
                    "allowed_domains": ["api.transportadora.co"],
                    "headers": {"Authorization": "Bearer s3cr3t0", "X-Cliente": "solvershub"},
                    "parameters": {"type": "object", "properties": {"guia": {"type": "string"}}},
                }
            ],
            "nota_interna": {"de": "migración"},
        },
    }


def _secciones(spec: AgentSpec, candidatas: list[str]) -> list[FlowSection]:
    propia = FlowSection(id="envios", label="Envíos", purpose="Rastrear guías", pack="tenant", tools=["consultar_envio"])
    compuesto = compose_flow(load_core_flow(), load_pack_flows(sorted(spec.packs)), [propia])
    disponibles = set(candidatas)
    return [
        FlowSection(s.id, s.label, s.purpose, s.pack, [t for t in s.tools if t in disponibles])
        for s in compuesto
        if any(t in disponibles for t in s.tools)
    ]


def _congelados(raw: dict[str, Any], catalogo: Mapping[str, ActionInfo]) -> tuple[AgenteCongelado, AgenteCongelado, AgentSpec]:
    """(General, Coordinador, v2 original) tras el lector tolerante, como quedarían en la release."""
    spec = AgentSpec.model_validate(raw)
    candidatas, siempre = candidatas_del_spec(spec, catalogo)
    conversion = convertir_v2(
        spec, tenant_id=str(T), catalogo=catalogo, secciones=_secciones(spec, candidatas), candidatas=candidatas, siempre=siempre
    )
    general = AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=KEY_GENERAL, role="specialist", version_id=uuid.uuid4(),
        version=3, contract="agentspec/3", content=conversion.general.model_dump(mode="json", by_alias=True),
    )
    kernel = AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=KEY_COORDINADOR, role="core", version_id=uuid.uuid4(),
        version=1, contract="kernelspec/1", content=conversion.kernel.model_dump(mode="json", by_alias=True),
    )
    return general, kernel, spec


# ---------------------------------------------------------------- ida y vuelta
def test_la_vista_v2_devuelve_lo_que_el_loop_lee(catalogo) -> None:
    general, kernel, original = _congelados(v2_completo(), catalogo)
    vista = spec_v2_desde_release(general, kernel, tenant_id=T, skills=("pack_commerce", "pack_payments"), tenant_plugins=())

    assert vista.tenantId == str(T) and vista.id == KEY_GENERAL and vista.version == "general@v3:3"
    # Persona: la voz de la conexión (KernelSpec) y el tope y las frases del agente.
    assert vista.persona.model_dump() == original.persona.model_dump()
    assert vista.model.model_dump() == original.model.model_dump()
    assert vista.memory.model_dump() == original.memory.model_dump()
    assert [p.name for p in vista.playbooks] == [p.name for p in original.playbooks]
    assert vista.policies.model_dump() == original.policies.model_dump()
    assert set(vista.packs) == set(original.packs)
    # Bindings: los del v2 iguales; los que el lector agregó (acciones ofrecidas sin binding) conceden
    # exactamente los scopes que la acción exige, así `_tool_denial` decide igual que sin binding.
    por_tool = {b.tool: b for b in vista.tools}
    for b in original.tools:
        assert por_tool[b.tool].model_dump() == b.model_dump()
    for nombre, b in por_tool.items():
        if nombre in {"crear_pedido", "send_email"}:
            continue
        assert b.enabled and b.maxCallsPerConversation is None and not b.requiresConfirmation
        # La declarativa del negocio (`consultar_envio`) no está en el catálogo del binario.
        assert set(b.scopes) >= set(catalogo[nombre].scopes if nombre in catalogo else ())
    # Guardrails y declarativas vuelven a `extensions` (lo único que v2 transportaba ahí).
    reglas = {g["name"]: g for g in vista.extensions["guardrails"]}
    assert reglas["gratis"]["action"] == "escalate" and reglas["link"]["tool"] == "confirmar_pago_pedido"
    assert reglas["largo"]["value"] == 900
    [declarativa] = vista.extensions["tools"]
    assert declarativa["name"] == "consultar_envio" and declarativa["allowed_domains"] == ["api.transportadora.co"]
    assert declarativa["headers"]["X-Cliente"] == "solvershub"
    # El secreto literal del v2 no vuelve: viaja por referencia (lo resuelve `declarative.py`).
    assert declarativa["headers"]["Authorization"] == {"secret": "consultar_envio_authorization"}
    assert "s3cr3t0" not in vista.model_dump_json()
    assert vista.extensions["nota_interna"] == {"de": "migración"}
    # `writeNamespaces` no viaja por la vista: va por la ranura (`release_vigente`).
    assert general.write_namespaces == frozenset({"perfil"})


def test_las_declarativas_con_referencias_en_url_o_body_se_omiten(catalogo) -> None:
    general, kernel, _ = _congelados(v2_completo(), catalogo)
    contenido = copy.deepcopy(dict(general.content))
    contenido["declarativeTools"] = [
        {
            "name": "consultar_envio",
            "method": "GET",
            "url": "https://api.transportadora.co/v1/guias?token={secret:transportadora_token}",
            "allowedDomains": ["api.transportadora.co"],
            "headers": {},
        },
        {
            "name": "notificar",
            "method": "POST",
            "url": "https://api.transportadora.co/v1/avisos",
            "allowedDomains": ["api.transportadora.co"],
            "body": {"api_key": {"secret": "transportadora_token"}},
        },
        {"name": "sin_secretos", "method": "GET", "url": "https://api.transportadora.co/v1/ok", "allowedDomains": ["api.transportadora.co"]},
    ]
    agente = AgenteCongelado(**{**general.__dict__, "content": contenido})
    from agentcore_contracts import AgentSpecV3

    assert declarativas_omitidas(AgentSpecV3.model_validate(contenido)) == ["consultar_envio", "notificar"]
    vista = spec_v2_desde_release(agente, kernel, tenant_id=T, skills=(), tenant_plugins=())
    assert [t["name"] for t in vista.extensions["tools"]] == ["sin_secretos"]


def test_un_contenido_que_no_pasa_el_contrato_es_fail_closed(catalogo) -> None:
    general, kernel, _ = _congelados(v2_completo(), catalogo)
    roto = AgenteCongelado(**{**general.__dict__, "content": {**dict(general.content), "model": {"primary": ""}}})
    with pytest.raises(VistaInvalida):
        spec_v2_desde_release(roto, kernel, tenant_id=T, skills=(), tenant_plugins=())
    with pytest.raises(VistaInvalida):
        spec_v2_desde_release(general, None, tenant_id=T, skills=(), tenant_plugins=())


# ---------------------------------------------------------------- agente en foco
def _agente(key: str, role: str = "specialist") -> AgenteCongelado:
    return AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=key, role=role, version_id=uuid.uuid4(), version=1,
        contract="kernelspec/1" if role == "core" else "agentspec/3", content={"memory": {"writeNamespaces": ["ventas"]}},
    )


def test_elegir_foco_dueno_valido_luego_por_defecto_luego_unico() -> None:
    core, general, ventas = _agente("coordinador", "core"), _agente("general"), _agente("ventas")
    kernel = {"routing": {"defaultAgent": "general"}}
    assert elegir_foco([core, general, ventas], kernel, None) is general
    assert elegir_foco([core, general, ventas], kernel, ventas.instance_id) is ventas
    # El dueño que ya no es especialista de la release no cuenta; tampoco el Coordinador.
    assert elegir_foco([core, general, ventas], kernel, uuid.uuid4()) is general
    assert elegir_foco([core, general, ventas], kernel, core.instance_id) is general
    # Sin agente por defecto: el único especialista; con varios, fail-closed.
    assert elegir_foco([core, ventas], {"routing": {"defaultAgent": "general"}}, None) is ventas
    with pytest.raises(ErrorDeRelease):
        elegir_foco([core, ventas, _agente("agenda")], {"routing": {"defaultAgent": "general"}}, None)
    with pytest.raises(ErrorDeRelease):
        elegir_foco([core], kernel, None)
    assert ventas.write_namespaces == frozenset({"ventas"}) and core.write_namespaces == frozenset({"ventas"})


# ---------------------------------------------------------------- la ranura del turno
@pytest.mark.asyncio
async def test_spec_del_turno_sin_release_es_el_mismo_y_con_release_la_vista(catalogo, monkeypatch) -> None:
    general, kernel, original = _congelados(v2_completo(), catalogo)
    monkeypatch.setattr("core.shared.release_vigente.get_current_tenant_id", lambda: T)
    assert spec_del_turno(original) is original
    assert release_vigente.write_namespaces_del_turno() is None

    @con_release
    async def turno() -> tuple[AgentSpec, frozenset[str] | None]:
        release_vigente.fijar(
            ArtefactosDeRelease(
                tenant_id=T, connection_id=C, release_id=uuid.uuid4(), lane="stable", idle_minutes=720,
                agentes=(kernel, general), foco=general, skills=("pack_commerce", "pack_payments"),
            )
        )
        return spec_del_turno(original), release_vigente.write_namespaces_del_turno()

    vista, namespaces = await turno()
    assert vista is not original and vista.version == "general@v3:3"
    assert vista.persona.model_dump() == original.persona.model_dump()
    assert namespaces == frozenset({"perfil"})
    # Fuera del turno la ranura queda vacía: el siguiente turno no hereda nada.
    assert spec_del_turno(original) is original
