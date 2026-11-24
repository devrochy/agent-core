"""HU-AOS-98 · reglas y nivel de IA de la recepción del Coordinador (ADR-0211). Sin base.

La configuración por defecto no cambia nada: ni el contenido de la vista ni el hash de la release.
"""

from __future__ import annotations

import pytest
from agentcore_contracts import AgentSpecV3

from core.agents.recepcion import contenido_de_recepcion
from core.coordinador import recepcion_config as rc
from core.llm import niveles
from core.releases import manifiesto as man

REGLA = {"name": "sin_descuentos", "kind": "forbidden_phrase", "phrases": ["descuento"], "action": "escalate"}


def test_sin_configuracion_la_vista_es_la_de_siempre() -> None:
    base = contenido_de_recepcion("deepseek-flash")
    assert contenido_de_recepcion("deepseek-flash", {}) == base
    assert contenido_de_recepcion("deepseek-flash", None) == base
    assert base["guardrails"] == [] and base["model"]["primary"] == "deepseek-flash"
    assert rc.para_release({}) == {} and rc.para_release(None) == {}
    assert rc.para_release({rc.CLAVE_NIVEL: "estandar", rc.CLAVE_REGLAS: []}) == {}


def test_las_reglas_entran_a_la_vista_y_el_nivel_fija_el_modelo() -> None:
    recepcion = rc.para_release({rc.CLAVE_REGLAS: [REGLA], rc.CLAVE_NIVEL: "avanzado"})
    contenido = contenido_de_recepcion("deepseek-flash", recepcion)
    spec = AgentSpecV3.model_validate(contenido)
    assert [r.name for r in spec.guardrails] == ["sin_descuentos"]
    modelos = niveles.modelos_de("avanzado")
    assert (spec.model.primary, spec.model.router) == (modelos.primary, modelos.router)


def test_el_manifiesto_omite_la_recepcion_vacia_y_cambia_el_hash_si_hay_reglas() -> None:
    def armar(recepcion):
        return man.armar(
            componentes=[], habilidades=[], plugins_del_negocio=[], overrides={}, flujo_del_negocio=None,
            app_settings={"assistant_name": "A"}, recepcion=recepcion,
        )

    sin = armar(None)
    assert "recepcion" not in sin and armar({}) == sin
    con = armar(rc.para_release({rc.CLAVE_REGLAS: [REGLA]}))
    assert "recepcion" in con and man.hash_de(con) != man.hash_de(sin)


@pytest.mark.parametrize(
    ("reglas", "ruta"),
    [
        ([{"name": "x", "kind": "forbidden_phrase", "phrases": []}], "guardrails[0]"),
        ([{"name": "x", "kind": "required_tool", "phrases": ["envío"], "tool": "cotizar"}], "guardrails[0].tool"),
        ([REGLA, REGLA], "guardrails[1].name"),
        ([{"name": "x", "kind": "max_length"}], "guardrails[0]"),
        ([{"name": "x", "kind": "forbidden_phrase", "phrases": ["a"]}], "guardrails[0].phrases"),
        ([{"name": "x", "kind": "max_length", "value": 1}], "guardrails[0].value"),
        ([{"name": "x", "kind": "forbidden_phrase", "phrases": [f"frase {i}" for i in range(11)]}], "guardrails[0].phrases"),
    ],
)
def test_reglas_invalidas_dan_un_problema_por_campo(reglas, ruta) -> None:
    with pytest.raises(rc.ReglasInvalidas) as exc:
        rc.validar_reglas(reglas)
    assert any(r.startswith(ruta) for r, _ in exc.value.problemas)


def test_required_tool_acepta_solo_las_tools_de_la_recepcion() -> None:
    ok = {"name": "pide_tel", "kind": "required_tool", "phrases": ["llámame"], "tool": "pedir_telefono"}
    assert rc.validar_reglas([ok])[0]["tool"] == "pedir_telefono"


def test_demasiadas_reglas() -> None:
    reglas = [{**REGLA, "name": f"r{i}"} for i in range(rc.MAX_REGLAS + 1)]
    with pytest.raises(rc.ReglasInvalidas):
        rc.validar_reglas(reglas)


def test_una_regla_guardada_que_ya_no_vale_rompe_el_build_no_el_turno() -> None:
    with pytest.raises(rc.ReglasInvalidas):
        rc.para_release({rc.CLAVE_REGLAS: [{"name": "x", "kind": "inventada"}]})


def test_una_release_con_reglas_que_ya_no_valen_atiende_sin_ellas_y_no_lanza() -> None:
    mala = {"guardrails": [{"name": "x", "kind": "inventada"}]}
    assert contenido_de_recepcion("deepseek-flash", mala) == contenido_de_recepcion("deepseek-flash")
