"""HU-AOS-46 / ADR-0139: PresetManifest v2, plantillas de agente, derivación y reglas (sin base)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import core.presets as presets_mod
from core.presets import (
    PlantillaAgente,
    PresetError,
    PresetManifest,
    list_presets,
    load_plantilla,
    load_preset,
    plantillas_del_preset,
)
from core.presets.agentes import agregar_reglas, derivar_contenido

AGENTES_POR_PRESET = {
    "comercio": ["ventas"],
    "restaurante": ["ventas"],
    "cobranza": ["cobranza"],
    "suscripciones": ["ventas", "agenda", "suscripciones"],
}


def test_presets_existentes_son_v2_con_sus_agentes() -> None:
    por_id = {p.id: p for p in list_presets()}
    for preset_id, agentes in AGENTES_POR_PRESET.items():
        manifest = por_id[preset_id]
        assert manifest.es_v2 and manifest.agentes == agentes and manifest.version >= 2
        for plantilla in plantillas_del_preset(manifest):
            assert set(plantilla.habilidades) <= set(manifest.packs)


def test_manifiesto_sin_agentes_es_v1() -> None:
    base = json.loads((presets_mod.PRESETS_DIR / "comercio.json").read_text(encoding="utf-8"))
    base.pop("agentes")
    assert not PresetManifest.model_validate(base).es_v2


def test_plantillas_validan_procedimientos_y_habilidades_reales() -> None:
    from core.kernel.procedures.de_pack import cargar

    for key in ("ventas", "agenda", "cobranza", "suscripciones"):
        plantilla = load_plantilla(key)
        publicados = {p.id for pack in [*plantilla.habilidades, *plantilla.opcionales] for p in cargar(pack)}
        assert set(plantilla.procedimientos) <= publicados, key
        assert all(r.id.startswith("preset_") for r in plantilla.reglas)


def test_plantilla_rechaza_prompts_keys_reservadas_y_procedimientos_ajenos() -> None:
    base = json.loads((presets_mod.PLANTILLAS_DIR / "agenda.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        PlantillaAgente.model_validate({**base, "prompt": "Eres el mejor vendedor"})
    with pytest.raises(ValidationError):
        PlantillaAgente.model_validate({**base, "key": "general"})
    with pytest.raises(ValidationError):
        PlantillaAgente.model_validate({**base, "procedimientos": ["pack_cobranza.promesa_de_pago"]})
    with pytest.raises(ValidationError):  # regla sin condición (DSL del contrato kernelspec/1)
        PlantillaAgente.model_validate({**base, "reglas": [{"id": "preset_x", "when": {}}]})


def test_plantilla_inexistente_o_key_insegura() -> None:
    with pytest.raises(PresetError):
        load_plantilla("no_existe")
    with pytest.raises(PresetError):
        load_plantilla("../comercio")


def test_preset_con_plantilla_de_un_pack_que_no_trae(tmp_path, monkeypatch) -> None:
    base = json.loads((presets_mod.PRESETS_DIR / "cobranza.json").read_text(encoding="utf-8"))
    (tmp_path / "raro.json").write_text(json.dumps({**base, "id": "raro", "agentes": ["agenda"]}), encoding="utf-8")
    monkeypatch.setattr(presets_mod, "PRESETS_DIR", tmp_path)
    with pytest.raises(PresetError):
        load_preset("raro")


# ---------------------------------------------------------------- derivación
def _general() -> dict[str, Any]:
    return {
        "model": {"primary": "modelo-x", "temperature": 0.2},
        "persona": {"inheritConnectionVoice": True, "maxLength": 400, "forbiddenPhrases": []},
        "memory": {"shortTermTurns": 20, "writeNamespaces": ["perfil"]},
        "policies": {"maxToolCallsPerTurn": 7},
        "playbooks": [{"name": "ventas"}, {"name": "citas"}, {"name": "propio_del_negocio"}],
        "tools": [
            {"tool": "crear_pedido", "requiresConfirmation": True},
            {"tool": "check_availability"},
            {"tool": "escalate_to_human"},
        ],
        "guardrails": [
            {"name": "g1", "kind": "required_tool", "tool": "crear_pedido", "phrases": ["pedido listo"]},
            {"name": "g2", "kind": "max_length", "value": 300},
        ],
        "declarativeTools": [{"name": "externa", "url": "https://x.test"}],
    }


def _catalogo() -> dict[str, Any]:
    return {
        "crear_pedido": SimpleNamespace(owner="pack_commerce"),
        "check_availability": SimpleNamespace(owner="pack_calendar"),
        "escalate_to_human": SimpleNamespace(owner="core"),
        "crear_lead": SimpleNamespace(owner="pack_commerce"),
    }


def _proc(pid: str, version: str = "1.2.0") -> Any:
    return SimpleNamespace(id=pid, version=version, owner=pid.split(".")[0], requires=[])


def test_derivar_contenido_acota_al_fin_del_agente() -> None:
    contenido, avisos = derivar_contenido(
        load_plantilla("agenda"),
        _general(),
        catalogo=_catalogo(),
        habilitados={"pack_commerce", "pack_calendar"},
        publicados=[_proc("pack_calendar.agendar_cita", "1.0.0"), _proc("pack_commerce.tomar_pedido")],
        playbooks_de_packs={"ventas", "citas"},
    )
    assert avisos == []
    assert contenido["procedures"] == [{"id": "pack_calendar.agendar_cita", "version": "^1.0", "overrides": None}]
    assert [b["tool"] for b in contenido["tools"]] == ["check_availability", "escalate_to_human"]
    assert [g["name"] for g in contenido["guardrails"]] == ["g2"]  # el required_tool de ventas no sigue
    assert [p["name"] for p in contenido["playbooks"]] == ["citas", "propio_del_negocio"]
    assert contenido["declarativeTools"] == []
    assert contenido["model"]["primary"] == "modelo-x" and contenido["memory"]["writeNamespaces"] == ["perfil"]
    assert contenido["description"] == load_plantilla("agenda").descripcion


def _general_con_leads() -> dict[str, Any]:
    general = _general()
    general["tools"] = [*general["tools"], {"tool": "crear_lead"}, {"tool": "generar_cotizacion"}]
    general["guardrails"] = [
        *general["guardrails"],
        {"name": "g3", "kind": "required_tool", "tool": "crear_lead", "phrases": ["te registré"]},
    ]
    return general


def _catalogo_con_leads() -> dict[str, Any]:
    return {
        **_catalogo(),
        "generar_cotizacion": SimpleNamespace(owner="pack_commerce"),
    }


def test_agenda_toma_solo_crear_lead_de_pack_commerce() -> None:
    """HU-AOS-22: con pack_commerce, Agenda captura el lead del prospecto (`crear_lead` y su
    guardrail) sin la caja de ventas (cotización, pedido) ni el playbook de ventas."""
    contenido, avisos = derivar_contenido(
        load_plantilla("agenda"),
        _general_con_leads(),
        catalogo=_catalogo_con_leads(),
        habilitados={"pack_commerce", "pack_calendar"},
        publicados=[_proc("pack_calendar.agendar_cita", "1.0.0")],
        playbooks_de_packs={"ventas", "citas"},
    )
    assert avisos == []
    assert [b["tool"] for b in contenido["tools"]] == ["check_availability", "escalate_to_human", "crear_lead"]
    assert [g["name"] for g in contenido["guardrails"]] == ["g2", "g3"]
    assert [p["name"] for p in contenido["playbooks"]] == ["citas", "propio_del_negocio"]


def test_agenda_sin_pack_commerce_no_tiene_crear_lead() -> None:
    contenido, avisos = derivar_contenido(
        load_plantilla("agenda"),
        _general_con_leads(),
        catalogo=_catalogo_con_leads(),
        habilitados={"pack_calendar"},
        publicados=[_proc("pack_calendar.agendar_cita", "1.0.0")],
        playbooks_de_packs={"ventas", "citas"},
    )
    assert avisos == []
    assert [b["tool"] for b in contenido["tools"]] == ["check_availability", "escalate_to_human"]


def test_ventas_sigue_tomando_su_opcional_completa() -> None:
    """Una opcional sin `acciones_opcionales` entra completa (pack_payments en Ventas)."""
    general = _general()
    general["tools"] = [{"tool": "crear_pedido"}, {"tool": "link_de_pago"}, {"tool": "estado_pago"}]
    catalogo = {
        "crear_pedido": SimpleNamespace(owner="pack_commerce"),
        "link_de_pago": SimpleNamespace(owner="pack_payments"),
        "estado_pago": SimpleNamespace(owner="pack_payments"),
    }
    contenido, _ = derivar_contenido(
        load_plantilla("ventas"),
        general,
        catalogo=catalogo,
        habilitados={"pack_commerce", "pack_payments"},
        publicados=[_proc("pack_commerce.tomar_pedido")],
        playbooks_de_packs=set(),
    )
    assert [b["tool"] for b in contenido["tools"]] == ["crear_pedido", "link_de_pago", "estado_pago"]


def test_acciones_opcionales_avisa_si_la_habilidad_no_publica_la_accion() -> None:
    plantilla = load_plantilla("agenda").model_copy(
        update={"acciones_opcionales": {"pack_commerce": ["crear_lead", "no_existe"]}}
    )
    _, avisos = derivar_contenido(
        plantilla,
        _general_con_leads(),
        catalogo=_catalogo_con_leads(),
        habilitados={"pack_commerce", "pack_calendar"},
        publicados=[_proc("pack_calendar.agendar_cita", "1.0.0")],
        playbooks_de_packs=set(),
    )
    assert avisos == ["acciones_opcionales de pack_commerce que esa habilidad no publica: ['no_existe']"]


@pytest.mark.parametrize(
    "acciones",
    [{"pack_payments": ["link_de_pago"]}, {"pack_commerce": []}, {"pack_calendar": ["check_availability"]}],
)
def test_acciones_opcionales_solo_de_opcionales_y_no_vacias(acciones: dict[str, list[str]]) -> None:
    from pydantic import ValidationError

    from core.presets import PlantillaAgente

    datos = load_plantilla("agenda").model_dump()
    datos["opcionales"] = [*datos["opcionales"], "pack_calendar"]  # obligatoria y opcional a la vez
    datos["acciones_opcionales"] = acciones
    with pytest.raises(ValidationError):
        PlantillaAgente.model_validate(datos)


def test_derivar_sin_procedimientos_ofrecibles_falla() -> None:
    with pytest.raises(PresetError):
        derivar_contenido(
            load_plantilla("agenda"),
            _general(),
            catalogo=_catalogo(),
            habilitados={"pack_calendar"},
            publicados=[],
            playbooks_de_packs=set(),
        )


# ---------------------------------------------------------------- reglas del Coordinador
def test_agregar_reglas_solo_las_nuevas_sin_tocar_las_del_dueno() -> None:
    from agentcore_contracts import KernelSpec

    kernel = KernelSpec().model_dump(mode="json", by_alias=True)
    kernel["routing"]["rules"] = [
        {"id": "preset_agenda_palabras", "when": {"keywordAny": ["turno"]}, "agent": "general"},
    ]
    nuevo, agregadas, avisos = agregar_reglas(kernel, [load_plantilla("agenda"), load_plantilla("ventas")])
    assert agregadas == ["preset_ventas_palabras"]
    assert avisos == ["la regla preset_agenda_palabras ya existe con otro destino: se respeta"]
    reglas = {r["id"]: r for r in nuevo["routing"]["rules"]}
    assert reglas["preset_agenda_palabras"]["agent"] == "general"
    assert reglas["preset_ventas_palabras"]["agent"] == "ventas"
    assert nuevo["routing"]["defaultAgent"] == "general"
    # Idempotente.
    assert agregar_reglas(nuevo, [load_plantilla("ventas")])[1] == []


def test_agregar_reglas_respeta_el_tope() -> None:
    from agentcore_contracts import KernelSpec

    kernel = KernelSpec().model_dump(mode="json", by_alias=True)
    kernel["routing"]["rules"] = [
        {"id": f"r{i}", "when": {"keywordAny": ["hola"]}, "agent": "general"} for i in range(50)
    ]
    nuevo, agregadas, avisos = agregar_reglas(kernel, [load_plantilla("ventas")])
    assert agregadas == [] and len(avisos) == 1 and len(nuevo["routing"]["rules"]) == 50


# ---------------------------------------------------------------- HU-AOS-22: rutas del preset suscripciones
def _primera_regla(texto: str) -> str | None:
    """Destino de la primera regla del preset `suscripciones` que cumple (orden del Coordinador)."""
    from agentcore_contracts import KernelSpec

    from core.kernel.router import Entrada, cumple

    kernel, _agregadas, _avisos = agregar_reglas(
        KernelSpec().model_dump(mode="json", by_alias=True), plantillas_del_preset(load_preset("suscripciones"))
    )
    reglas = KernelSpec.model_validate(kernel).routing.rules
    return next((r.agent for r in reglas if cumple(r, Entrada(texto=texto))), None)


def test_suscripciones_trae_la_regla_de_mi_plan_con_id_nuevo() -> None:
    # Las reglas de una plantilla evolucionan con ids nuevos (adenda HU-AOS-22 de ADR-0139): a un
    # negocio que ya tiene `preset_suscripciones_palabras` le llega la nueva al reaplicar.
    ids = [r.id for r in load_plantilla("suscripciones").reglas]
    assert ids == ["preset_suscripciones_palabras", "preset_suscripciones_mi_plan"]
    from agentcore_contracts import KernelSpec

    kernel = KernelSpec().model_dump(mode="json", by_alias=True)
    kernel["routing"]["rules"] = [
        {"id": "preset_suscripciones_palabras", "when": {"keywordAny": ["mi suscripción"]}, "agent": "suscripciones"},
    ]
    nuevo, agregadas, _ = agregar_reglas(kernel, [load_plantilla("suscripciones")])
    assert agregadas == ["preset_suscripciones_mi_plan"]
    assert [r["id"] for r in nuevo["routing"]["rules"]][-1] == "preset_suscripciones_mi_plan"


@pytest.mark.parametrize(
    ("texto", "destino"),
    [
        ("quiero agendar una demo", "agenda"),
        ("cambiar mi plan", "suscripciones"),
        ("hola, necesito cambiar mi plan al premium porque crecimos", "suscripciones"),
        ("¿cuál es mi plan actual?", "suscripciones"),
        # «precio» es de Ventas y su regla va antes: decisión revisable, queda explícita.
        ("cuál es el precio para cambiar mi plan", "ventas"),
        # Sin palabra de regla: lo decide el disparador (contratar_plan → Ventas) o el General.
        ("quiero contratar Premium", None),
        ("hola, buenas tardes", None),
    ],
)
def test_reglas_del_preset_suscripciones_en_orden(texto: str, destino: str | None) -> None:
    assert _primera_regla(texto) == destino


def test_acciones_opcionales_de_las_plantillas_existen_en_su_habilidad() -> None:
    """Un typo en `acciones_opcionales` dejaría al agente sin la acción en silencio (solo un aviso al
    derivar): cada acción de las plantillas reales es del pack que la nombra."""
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    catalogo = catalogo_de_acciones(plugin_manager.scan())
    for archivo in sorted(presets_mod.PLANTILLAS_DIR.glob("*.json")):
        plantilla = load_plantilla(archivo.stem)
        for pack, acciones in plantilla.acciones_opcionales.items():
            for accion in acciones:
                info = catalogo.get(accion)
                assert info is not None and info.owner == pack, (plantilla.key, pack, accion)
