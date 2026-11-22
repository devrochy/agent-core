"""HU-AOS-89 · vista de recepción del Coordinador (ADR-0205 §5).

Sin base: la vista es un `agentspec/3` válido con el set cerrado de 7 tools, conserva los ids reales del Coordinador,
no trae packs ni procedimientos, se anota aparte en el ledger, entra al prompt con su playbook del kernel (y no con
el de Captar leads) y `update_customer_profile` desde la recepción solo guarda nombre, teléfono y correo.
"""

from __future__ import annotations

import uuid

from agentcore_contracts import AgentSpecV3
from test_agente_del_turno import v2_completo

from core.agents.base import kernel_del_negocio
from core.agents.claves import KEY_COORDINADOR
from core.agents.defaults_negocio import DefaultsDelNegocio
from core.agents.recepcion import (
    ATRIBUTOS_DE_PERFIL,
    TOOLS_RECEPCION,
    atributos_de_recepcion,
    es_vista_de_recepcion,
    vista_de_recepcion,
)
from core.agents.turno import agente_del_turno
from core.shared.release_vigente import AgenteCongelado

T = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _coordinador() -> AgenteCongelado:
    defaults = DefaultsDelNegocio.model_validate(v2_completo())
    return AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key=KEY_COORDINADOR, role="core", version_id=uuid.uuid4(),
        version=4, contract="kernelspec/1", content=kernel_del_negocio(defaults),
    )


def test_la_vista_es_un_agentspec_v3_con_el_set_cerrado() -> None:
    coordinador = _coordinador()
    vista = vista_de_recepcion(coordinador, modelo="deepseek-flash")
    AgentSpecV3.model_validate(vista.content)
    assert vista.tools == frozenset(TOOLS_RECEPCION) and len(TOOLS_RECEPCION) == 7
    assert vista.write_namespaces == frozenset({"perfil"})
    assert vista.declarativas == frozenset()
    assert (vista.packs, vista.procedimientos) == (frozenset(), ())
    assert vista.flow is not None and vista.flow["sections"] == []
    # Los ids reales: el ledger y la procedencia tienen FK a `agent_versions`.
    assert (vista.instance_id, vista.agent_id, vista.version_id, vista.role, vista.key) == (
        coordinador.instance_id, coordinador.agent_id, coordinador.version_id, "core", KEY_COORDINADOR,
    )
    assert es_vista_de_recepcion(vista) and not es_vista_de_recepcion(coordinador)
    for prohibida in ("crear_pedido", "create_calendar_event", "cob_registrar_promesa_pago", "sub_inscribir"):
        assert prohibida not in vista.tools


def test_un_especialista_no_se_convierte_en_vista() -> None:
    especialista = AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key="ventas", role="specialist", version_id=uuid.uuid4(),
        version=1, contract="agentspec/3", content={},
    )
    assert vista_de_recepcion(especialista, modelo="x") is especialista


def test_el_agente_del_turno_de_recepcion_no_tiene_packs_y_se_anota_aparte() -> None:
    coordinador = _coordinador()
    vista = vista_de_recepcion(coordinador, modelo="deepseek-flash")
    spec = agente_del_turno(vista, coordinador, skills=("pack_commerce",), tenant_plugins=())
    assert spec.version == f"coordinador@recepcion:{coordinador.version}"
    assert spec.packs == () and spec.recepcion
    assert {t.tool for t in spec.tools if t.enabled} == set(TOOLS_RECEPCION)
    assert [p.name for p in spec.playbooks] == ["respuestas"]


async def test_el_prompt_lleva_el_playbook_de_recepcion_y_no_el_de_captar_leads() -> None:
    from core.agents.playbooks import load_core_playbook
    from core.llm.prompt_builder import PromptContext, bloques_fijos

    coordinador = _coordinador()
    spec = agente_del_turno(vista_de_recepcion(coordinador, modelo="m"), coordinador, skills=(), tenant_plugins=())
    bloques = await bloques_fijos(spec, PromptContext(assistant_name="Ana", business_description="Tienda"), tenant_slug=None)
    recepcion = load_core_playbook("recepcion-coordinador").strip()
    assert recepcion in bloques
    assert load_core_playbook("captar-leads").strip() not in bloques
    assert "crear_lead" in recepcion and "actualizar_lead" not in recepcion


def test_la_recepcion_solo_guarda_contacto_en_el_perfil() -> None:
    args = {"attributes": {"name": "Ana", "phone": "300", "email": "a@x.co", "direccion": "Calle 1", "cedula": "9"}}
    recortados, fuera = atributos_de_recepcion(args)
    assert recortados["attributes"] == {"name": "Ana", "phone": "300", "email": "a@x.co"}
    assert fuera == ["cedula", "direccion"]
    assert set(recortados["attributes"]) <= ATRIBUTOS_DE_PERFIL
    assert atributos_de_recepcion({"attributes": {"name": "Ana"}}) == ({"attributes": {"name": "Ana"}}, [])
    # Una lista de pares la aceptaría la tool (`dict([...])`): desde la recepción no pasa nada.
    pares, fuera = atributos_de_recepcion({"attributes": [["direccion", "x"], ["vip", "si"]]})
    assert pares["attributes"] == {} and fuera == ["attributes"]
