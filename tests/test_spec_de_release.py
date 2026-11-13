"""HU-AOS-15 · la vista v2 de la release (`core/agents/spec_de_release.py`) y el agente en foco.

Unitarios, sin BD ni red. Un especialista nativo (`agentspec/3` desde la configuración del
negocio, `core.agents.base`, ADR-0192) y su Coordinador (`kernelspec/1`): la vista lo devuelve como
lo lee el loop, campo por campo en lo que el loop usa. Más las reglas de `elegir_foco`, la omisión
fail-closed de las declarativas con referencias en la URL o el body, y la ranura del turno.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from agentcore_contracts import AgentSpec

from core.agents.base import contenido_base, kernel_del_negocio
from core.agents.claves import KEY_COORDINADOR
from core.agents.spec_de_release import (
    VistaInvalida,
    declarativas_omitidas,
    spec_del_turno,
    spec_v2_desde_release,
)
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


KEY = "atencion"


def _congelados(raw: dict[str, Any], catalogo: Mapping[str, ActionInfo]) -> tuple[AgenteCongelado, AgenteCongelado, AgentSpec]:
    """(especialista, Coordinador, v2 de la configuración) como quedarían en la release: la base del
    especialista sale de la configuración y la plantilla le pone bindings, reglas y declarativas."""
    del catalogo
    spec = AgentSpec.model_validate(raw)
    contenido = contenido_base(spec)
    contenido["tools"] = [b.model_dump(mode="json") for b in spec.tools]
    contenido["guardrails"] = [
        {"name": "gratis", "kind": "forbidden_phrase", "phrases": ["gratis para siempre"], "action": "escalate"},
        {"name": "link", "kind": "required_tool", "phrases": ["link de pago"], "tool": "confirmar_pago_pedido"},
        {"name": "largo", "kind": "max_length", "value": 900},
    ]
    contenido["declarativeTools"] = [
        {
            "name": "consultar_envio",
            "description": "Estado de una guía",
            "url": "https://api.transportadora.co/v1/guias/{guia}",
            "allowedDomains": ["api.transportadora.co"],
            "headers": {"Authorization": {"secret": "consultar_envio_authorization"}, "X-Cliente": "solvershub"},
            "parameters": {"type": "object", "properties": {"guia": {"type": "string"}}},
        }
    ]
    especialista = AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=KEY, role="specialist", version_id=uuid.uuid4(),
        version=3, contract="agentspec/3", content=contenido,
    )
    kernel = AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=KEY_COORDINADOR, role="core", version_id=uuid.uuid4(),
        version=1, contract="kernelspec/1", content=kernel_del_negocio(spec),
    )
    return especialista, kernel, spec


# ---------------------------------------------------------------- la vista
def test_la_vista_v2_devuelve_lo_que_el_loop_lee(catalogo) -> None:
    especialista, kernel, original = _congelados(v2_completo(), catalogo)
    vista = spec_v2_desde_release(especialista, kernel, tenant_id=T, skills=("pack_commerce", "pack_payments"), tenant_plugins=())

    assert vista.tenantId == str(T) and vista.id == KEY and vista.version == "atencion@v3:3"
    # Persona: la voz de la conexión (KernelSpec) y el tope y las frases del agente.
    assert vista.persona.model_dump() == original.persona.model_dump()
    assert vista.model.model_dump() == original.model.model_dump()
    assert vista.memory.model_dump() == original.memory.model_dump()
    assert [p.name for p in vista.playbooks] == [p.name for p in original.playbooks]
    assert vista.policies.model_dump() == original.policies.model_dump()
    assert set(vista.packs) == set(original.packs)
    # Bindings: exactamente los del agente.
    assert [b.model_dump() for b in vista.tools] == [b.model_dump() for b in original.tools]
    # Guardrails y declarativas vuelven a `extensions` (lo único que v2 transportaba ahí).
    reglas = {g["name"]: g for g in vista.extensions["guardrails"]}
    assert reglas["gratis"]["action"] == "escalate" and reglas["link"]["tool"] == "confirmar_pago_pedido"
    assert reglas["largo"]["value"] == 900
    [declarativa] = vista.extensions["tools"]
    assert declarativa["name"] == "consultar_envio" and declarativa["allowed_domains"] == ["api.transportadora.co"]
    assert declarativa["headers"]["X-Cliente"] == "solvershub"
    # El secreto viaja por referencia (lo resuelve `declarative.py`).
    assert declarativa["headers"]["Authorization"] == {"secret": "consultar_envio_authorization"}
    # `writeNamespaces` no viaja por la vista: va por la ranura (`release_vigente`).
    assert especialista.write_namespaces == frozenset({"perfil"})


def test_las_declarativas_con_referencias_en_url_o_body_se_omiten(catalogo) -> None:
    especialista, kernel, _ = _congelados(v2_completo(), catalogo)
    contenido = copy.deepcopy(dict(especialista.content))
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
    agente = AgenteCongelado(**{**especialista.__dict__, "content": contenido})
    from agentcore_contracts import AgentSpecV3

    assert declarativas_omitidas(AgentSpecV3.model_validate(contenido)) == ["consultar_envio", "notificar"]
    vista = spec_v2_desde_release(agente, kernel, tenant_id=T, skills=(), tenant_plugins=())
    assert [t["name"] for t in vista.extensions["tools"]] == ["sin_secretos"]


def test_un_contenido_que_no_pasa_el_contrato_es_fail_closed(catalogo) -> None:
    especialista, kernel, _ = _congelados(v2_completo(), catalogo)
    roto = AgenteCongelado(**{**especialista.__dict__, "content": {**dict(especialista.content), "model": {"primary": ""}}})
    with pytest.raises(VistaInvalida):
        spec_v2_desde_release(roto, kernel, tenant_id=T, skills=(), tenant_plugins=())
    with pytest.raises(VistaInvalida):
        spec_v2_desde_release(especialista, None, tenant_id=T, skills=(), tenant_plugins=())


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
    especialista, kernel, original = _congelados(v2_completo(), catalogo)
    monkeypatch.setattr("core.shared.release_vigente.get_current_tenant_id", lambda: T)
    assert spec_del_turno(original) is original
    assert release_vigente.write_namespaces_del_turno() is None

    @con_release
    async def turno() -> tuple[AgentSpec, frozenset[str] | None]:
        release_vigente.fijar(
            ArtefactosDeRelease(
                tenant_id=T, connection_id=C, release_id=uuid.uuid4(), lane="stable", idle_minutes=720,
                agentes=(kernel, especialista), foco=especialista, skills=("pack_commerce", "pack_payments"),
            )
        )
        return spec_del_turno(original), release_vigente.write_namespaces_del_turno()

    vista, namespaces = await turno()
    assert vista is not original and vista.version == "atencion@v3:3"
    assert vista.persona.model_dump() == original.persona.model_dump()
    assert namespaces == frozenset({"perfil"})
    # Fuera del turno la ranura queda vacía: el siguiente turno no hereda nada.
    assert spec_del_turno(original) is original


@pytest.mark.asyncio
async def test_write_namespaces_legacy_none_y_v3_sin_release_niega_todo() -> None:
    """Revisión de seguridad: un negocio v3 sin release activa no escribe memoria (fail-closed);
    legacy sigue sin chequeo (None); otro negocio en el mismo contexto no hereda la marca."""
    from core.shared.tenant import tenant_scope

    token = release_vigente.abrir()
    try:
        async with tenant_scope(T):
            assert release_vigente.write_namespaces_del_turno() is None
            release_vigente.marcar_v3_sin_release(T)
            assert release_vigente.write_namespaces_del_turno() == frozenset()
        async with tenant_scope(uuid.uuid4()):
            assert release_vigente.write_namespaces_del_turno() is None
    finally:
        release_vigente.cerrar(token)
    assert release_vigente.write_namespaces_del_turno() is None
