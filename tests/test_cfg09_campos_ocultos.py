"""HU-CFG-09: los campos marcados `oculto` no salen en el formulario y conservan lo guardado."""

from __future__ import annotations

from pack_cobranza.config import CobranzaConfig
from pydantic import BaseModel, Field

from core.admin.form_generator import parse_json_schema_form, render_json_schema_form
from core.api.forms import coerce_schema_values, hide_fields

OCULTAS = {"asistente_activo", "escalar_siempre_a_humano"}


class _Config(BaseModel):
    visible: int = Field(default=1)
    muerto: bool = Field(default=True, json_schema_extra={"oculto": True})


def test_cobranza_marca_las_dos_claves_que_no_hacen_nada() -> None:
    props = CobranzaConfig.model_json_schema()["properties"]
    assert {n for n, p in props.items() if p.get("oculto")} == OCULTAS


def test_el_formulario_no_pinta_los_campos_ocultos() -> None:
    schema, valores = hide_fields(CobranzaConfig.model_json_schema(), CobranzaConfig().model_dump(mode="json"))
    assert OCULTAS.isdisjoint(schema["properties"]) and OCULTAS.isdisjoint(valores)
    assert "umbral_mora_dias" in schema["properties"]
    html = render_json_schema_form(CobranzaConfig.model_json_schema())
    assert 'name="asistente_activo"' not in html and 'name="umbral_mora_dias"' in html


def test_sin_campos_ocultos_no_cambia_nada() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    assert hide_fields(schema, {"a": 1}) == (schema, {"a": 1})


def test_el_formulario_clasico_no_pisa_los_booleanos_ocultos_ausentes() -> None:
    """El panel SSR (aún activo) no manda un campo que no pinta: no debe leerse como «apagado»."""
    schema = CobranzaConfig.model_json_schema()
    previo = {"asistente_activo": False, "escalar_siempre_a_humano": True}
    valores = parse_json_schema_form(schema, {"umbral_mora_dias": "7"}, previo)
    assert valores["asistente_activo"] is False and valores["escalar_siempre_a_humano"] is True
    assert valores["umbral_mora_dias"] == 7
    # Sin valor previo, el de fábrica.
    sin_previo = parse_json_schema_form(schema, {})
    assert sin_previo["asistente_activo"] is True and sin_previo["escalar_siempre_a_humano"] is False


def test_guardar_conserva_lo_guardado_y_ignora_lo_que_llegue() -> None:
    schema = _Config.model_json_schema()
    # Con valor previo: se conserva aunque el cliente mande otro.
    assert coerce_schema_values(schema, {"visible": 5, "muerto": True}, {"muerto": False}) == {
        "visible": 5,
        "muerto": False,
    }
    # Sin valor previo: el default del esquema.
    assert coerce_schema_values(schema, {"visible": 5}, {}) == {"visible": 5, "muerto": True}


# ------------------------------------------------------------------ ayudas de Cerebro, Memoria, Chat web y Multimedia
def _limites(modelo: type[BaseModel], campo: str) -> tuple[float | None, float | None]:
    info = modelo.model_fields[campo]
    ge = next((m.ge for m in info.metadata if hasattr(m, "ge")), None)
    le = next((m.le for m in info.metadata if hasattr(m, "le")), None)
    return ge, le


def test_las_cuatro_secciones_traen_sus_opciones_con_ayuda() -> None:
    from core.config_registro import POR_CLAVE

    esperadas = {
        "cerebro": {"model_primary", "model_router", "model_fallback", "model_temperature", "policy_max_tool_calls", "policy_budget"},
        "memoria": {"short_term_turns", "rag_top_k", "rag_min_score"},
        "chat_web": {"web_chat_enabled", "web_chat_allowed_origins"},
        "multimedia": {"audio_enabled", "vision_enabled", "vision_mode"},
    }
    for grupo, claves in esperadas.items():
        assert {c for c, o in POR_CLAVE.items() if o.grupo == grupo} == claves
    assert all(len(POR_CLAVE[c].explicacion) >= 20 for claves in esperadas.values() for c in claves)


def test_los_rangos_de_cerebro_y_memoria_son_los_que_valida_la_api() -> None:
    from core.api.routers.settings import BrainUpdate, MemoryUpdate
    from core.config_registro import POR_CLAVE

    for clave, modelo in [
        ("model_temperature", BrainUpdate),
        ("policy_max_tool_calls", BrainUpdate),
        ("policy_budget", BrainUpdate),
        ("short_term_turns", MemoryUpdate),
        ("rag_top_k", MemoryUpdate),
        ("rag_min_score", MemoryUpdate),
    ]:
        o = POR_CLAVE[clave]
        assert _limites(modelo, clave) == (o.minimo, o.maximo), clave


def test_los_defectos_salen_del_spec_por_defecto() -> None:
    from core.agents.spec import DEFAULT_SPEC
    from core.config_registro import POR_CLAVE

    assert POR_CLAVE["model_primary"].por_defecto == DEFAULT_SPEC["model"]["primary"]
    assert POR_CLAVE["policy_budget"].por_defecto == DEFAULT_SPEC["policies"]["budgetUsdPerConversation"]
    assert POR_CLAVE["short_term_turns"].por_defecto == DEFAULT_SPEC["memory"]["shortTermTurns"]


def test_el_catalogo_ya_no_dice_el_runtime() -> None:
    from core.config_registro import catalogo_para

    assert "aos_runtime" not in catalogo_para(es_master=False, guardrails_disponibles=[]).model_dump()
