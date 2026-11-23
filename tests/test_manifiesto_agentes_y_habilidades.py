"""Contrato del manifiesto del tipo de agente y del catálogo Habilidad → Tools (ADR-0207 §1–§4, HU-AOS-92)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from core.agents.tipos_de_agente import TipoDeAgenteInvalido, _leer, tipos_de_agente
from core.kernel.procedures.catalogo import (
    CORE_OWNER,
    _core_skills,
    _skills_del_core_fuera_del_linter,
    catalogo_de_acciones,
    catalogo_de_habilidades,
    validar_catalogo,
)
from core.kernel.procedures.de_pack import cargar
from core.kernel.procedures.linter import lint
from core.kernel.release_del_turno import tuvo_efecto_irreversible
from core.mcp.types import AgentTypeManifest, MCPTool
from core.presets import load_plantilla
from core.releases.packs import es_de_plataforma, manifiesto_de_pack

ESPECIALIDADES = {"pack_commerce", "pack_calendar", "pack_cobranza", "pack_subscriptions"}
CAMPOS_DEL_TIPO = {
    "key", "nombre", "descripcion", "seccion", "habilidades_obligatorias", "habilidades_opcionales",
    "procedimientos_por_defecto", "playbooks_por_defecto", "flujo_por_defecto",
}


@pytest.fixture(scope="module")
def modulos() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return [m for m in plugin_manager.scan() if es_de_plataforma(m)]


def _tools_de_la_plataforma(modulos: list[Any]) -> list[MCPTool]:
    tools = [t for s in [*_core_skills(), *_skills_del_core_fuera_del_linter()] for t in s.get_tools()]
    for module in modulos:
        for skill_cls in getattr(module, "declared_skills", None) or []:
            tools.extend(skill_cls().get_tools())
    return tools


# ------------------------------------------------------------- el pack declara su tipo
def test_cada_pack_de_especialidad_expone_su_tipo(modulos) -> None:
    """Gherkin «El pack de especialidad declara su tipo de agente»."""
    por_pack = {m.id: m.tipo_de_agente for m in modulos if m.tipo == "especialidad"}
    assert set(por_pack) == ESPECIALIDADES
    assert {t.key for t in por_pack.values() if t is not None} == {"ventas", "agenda", "cobranza", "suscripciones"}
    for pack, tipo in por_pack.items():
        assert isinstance(tipo, AgentTypeManifest), pack
        assert CAMPOS_DEL_TIPO <= set(tipo.model_dump()), pack
        assert pack in tipo.habilidades_obligatorias, pack
    assert all(m.tipo_de_agente is None for m in modulos if m.tipo != "especialidad")


def test_el_registro_de_archivos_y_el_de_modulos_coinciden(modulos) -> None:
    """`load_plantilla` lee los archivos de `packages/` y el arranque los módulos cargados: son el mismo registro."""
    assert tipos_de_agente() == tipos_de_agente(modulos)


def test_load_plantilla_conserva_la_forma_de_antes() -> None:
    """Los lectores de `PlantillaAgente` no cambian: el tipo de `ventas` se traduce 1:1."""
    ventas = load_plantilla("ventas")
    assert ventas.habilidades == ["pack_commerce"] and ventas.opcionales == ["pack_payments"]
    assert ventas.procedimientos[0] == "pack_commerce.tomar_pedido" and ventas.captar_leads


def test_las_habilidades_del_core_no_llegan_a_la_plantilla() -> None:
    """`PlantillaAgente` solo lleva packs (lo que se habilita e instala): un `core:*` del tipo no la rompe."""
    from core.agents.tipos_de_agente import a_plantilla

    agenda = tipos_de_agente()["agenda"]
    con_core = agenda.model_copy(update={"habilidades_opcionales": ["core:leads"]})
    assert a_plantilla(con_core).opcionales == []


def test_el_tipo_va_en_el_manifiesto_congelado_del_pack(modulos) -> None:
    commerce = next(m for m in modulos if m.id == "pack_commerce")
    provee = manifiesto_de_pack(commerce).manifest["provides"]
    assert [t["key"] for t in provee["agent_templates"]] == ["ventas"]
    assert all(t["side_effect"] in {"read", "write", "irreversible"} for t in provee["tools"])


def test_flujo_por_defecto_con_una_seccion_inexistente_falla(tmp_path) -> None:
    carpeta = tmp_path / "pack-x" / "pack_x"
    carpeta.mkdir(parents=True)
    (tmp_path / "pack-x" / "flow.json").write_text(json.dumps({"sections": [{"id": "uno"}]}), encoding="utf-8")
    base = load_plantilla("agenda")
    manifiesto = {
        "key": "x_prueba", "nombre": "X", "habilidades_obligatorias": ["pack_calendar"],
        "procedimientos_por_defecto": base.procedimientos, "flujo_por_defecto": ["dos"],
    }
    (carpeta / "tipo_de_agente.json").write_text(json.dumps(manifiesto), encoding="utf-8")
    with pytest.raises(TipoDeAgenteInvalido, match="dos"):
        _leer(str(carpeta / "tipo_de_agente.json"))


def test_el_manifiesto_no_admite_campos_desconocidos() -> None:
    base = tipos_de_agente()["cobranza"].model_dump()
    AgentTypeManifest.model_validate(base)  # el manifiesto real es válido: el error es solo por `prompt`
    with pytest.raises(ValueError, match="prompt"):
        AgentTypeManifest.model_validate({**base, "prompt": "Eres el mejor cobrador"})


# ------------------------------------------------------------- cada habilidad declara sus tools
def test_catalogo_de_habilidades_con_sus_tools(modulos) -> None:
    """Gherkin «Cada habilidad declara sus tools y metadatos»."""
    catalogo = {h.id: h for h in catalogo_de_habilidades(modulos)}
    assert {"core:datos_cliente", "core:operacion", "core:leads", "core:recepcion"} <= set(catalogo)
    assert {"pack_commerce", "pack_calendar", "pack_cobranza", "pack_subscriptions", "pack_knowledge", "pack_payments"} <= set(catalogo)
    assert {t.name for t in catalogo["core:leads"].tools} == {"crear_lead", "calificar_lead", "actualizar_lead"}
    # Las `sub_*` del core son de la habilidad Suscripciones; su dueño en runtime sigue siendo el core.
    subs = catalogo["pack_subscriptions"].tools
    assert subs and all(t.name.startswith("sub_") and t.owner == CORE_OWNER for t in subs)
    for habilidad in catalogo.values():
        for tool in habilidad.tools:
            assert tool.side_effect in {"read", "write", "irreversible"}, (habilidad.id, tool.name)
            assert tool.description, (habilidad.id, tool.name)
    for pack in ESPECIALIDADES - {"pack_subscriptions"}:
        assert all(t.group for t in catalogo[pack].tools), pack


def test_ninguna_tool_en_dos_habilidades_salvo_la_recepcion(modulos) -> None:
    vistas: dict[str, str] = {}
    for habilidad in catalogo_de_habilidades(modulos):
        if habilidad.id == "core:recepcion":
            continue
        for tool in habilidad.tools:
            assert tool.name not in vistas, (tool.name, vistas.get(tool.name), habilidad.id)
            vistas[tool.name] = habilidad.id


# ------------------------------------------------------------- validación
def test_registro_valido_al_arrancar(modulos) -> None:
    """Gherkin «Validación de integridad en arranque y CI»."""
    assert validar_catalogo(modulos) == []


def test_validacion_detecta_tool_sin_efecto_o_sin_grupo(modulos, monkeypatch) -> None:
    import dataclasses

    calendar = next(m for m in modulos if m.id == "pack_calendar")
    skill_cls = calendar.declared_skills[0]
    originales = skill_cls.get_tools

    def _rotas(self):
        tools = originales(self)
        return [dataclasses.replace(tools[0], side_effect=None), *tools[1:]]

    monkeypatch.setattr(skill_cls, "get_tools", _rotas)
    monkeypatch.setitem(type(calendar).grupos_de_acciones, "nueva_sin_grupo", "x")
    monkeypatch.delitem(type(calendar).grupos_de_acciones, "check_availability")
    problemas = validar_catalogo(modulos)
    assert any("no declara side_effect" in p for p in problemas)
    assert any("'check_availability'" in p and "no tiene grupo" in p for p in problemas)


def test_validacion_detecta_habilidad_opcional_inexistente(modulos, monkeypatch) -> None:
    import core.agents.tipos_de_agente as tipos_mod

    reales = tipos_mod.tipos_de_agente(modulos)
    roto = reales["ventas"].model_copy(update={"habilidades_opcionales": ["pack_no_existe"]})
    monkeypatch.setattr(tipos_mod, "tipos_de_agente", lambda _m=None: {**reales, "ventas": roto})
    assert any("'pack_no_existe'" in p for p in validar_catalogo(modulos))


def test_validacion_detecta_procedimiento_con_accion_ajena(modulos, monkeypatch) -> None:
    import core.agents.tipos_de_agente as tipos_mod

    reales = tipos_mod.tipos_de_agente(modulos)
    # Agenda con el procedimiento de Ventas, cuyas acciones son de pack_commerce.
    roto = reales["agenda"].model_copy(update={"procedimientos_por_defecto": ["pack_commerce.tomar_pedido"]})
    monkeypatch.setattr(tipos_mod, "tipos_de_agente", lambda _m=None: {**reales, "agenda": roto})
    assert any("no está en las habilidades del tipo" in p for p in validar_catalogo(modulos))


def test_validacion_detecta_tool_del_core_sin_habilidad(modulos, monkeypatch) -> None:
    import core.kernel.procedures.catalogo as catalogo_mod

    textos = catalogo_mod._textos_del_core()
    sin_leads = {k: ({**v, "tools": []} if k == "core:leads" else v) for k, v in textos.items()}
    monkeypatch.setattr(catalogo_mod, "_textos_del_core", lambda: sin_leads)
    assert any("'crear_lead'" in p and "ninguna habilidad del core" in p for p in validar_catalogo(modulos))


def test_validacion_ignora_los_plugins_de_un_negocio(modulos) -> None:
    class PluginDelNegocio:
        id = "plugin_tenant"
        tipo = ""
        declared_skills: tuple[Any, ...] = ()

    assert validar_catalogo([*modulos, PluginDelNegocio()]) == []


# ------------------------------------------------------------- side_effect
def test_side_effect_coherente_con_los_otros_metadatos(modulos) -> None:
    """`solo_lectura` ⇔ `read`, y una tool con efectos en el canal `test` nunca es `read` (revisión del arquitecto)."""
    for tool in _tools_de_la_plataforma(modulos):
        assert tool.side_effect is not None, tool.name
        assert tool.solo_lectura == (tool.side_effect == "read"), tool.name
        if tool.side_effects:
            assert tool.side_effect != "read", tool.name


def test_los_procedimientos_publicados_siguen_validos_sin_subir_version(modulos) -> None:
    """Las releases ya publicadas se vuelven a lintear en runtime (`registro._valido`): declarar los efectos no
    puede dejar una irreversible sin confirmar en un procedimiento de pack."""
    catalogo = catalogo_de_acciones(modulos)
    publicados = [p for m in modulos for p in cargar(m.id)]
    assert len(publicados) == 7
    for proc in publicados:
        assert lint(proc, catalogo) == [], proc.id


@dataclass
class _Ev:
    type: str
    data: dict[str, Any]


def test_cambio_de_release_cancela_si_solo_leyo_o_escribio_y_escala_si_fue_irreversible(modulos) -> None:
    """ADR-0207, consecuencias: al cambiar la release, una tarea sin efecto irreversible se cancela (antes, con todo
    el catálogo en `None`, se escalaba siempre); con uno irreversible o desconocido se sigue escalando."""
    catalogo = catalogo_de_acciones(modulos)
    leyo_y_escribio = [
        _Ev("action_succeeded", {"action": "check_availability"}),
        _Ev("action_succeeded", {"action": "create_calendar_event"}),
    ]
    assert catalogo["check_availability"].side_effect == "read"
    assert catalogo["create_calendar_event"].side_effect == "write"
    assert not tuvo_efecto_irreversible(leyo_y_escribio, catalogo)
    assert tuvo_efecto_irreversible([*leyo_y_escribio, _Ev("action_succeeded", {"action": "cob_registrar_abono"})], catalogo)
    assert tuvo_efecto_irreversible([_Ev("action_succeeded", {"action": "accion_que_ya_no_existe"})], catalogo)
