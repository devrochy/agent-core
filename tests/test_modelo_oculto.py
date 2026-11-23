"""HU-BILL-24 · ADR-0210 §1: el modelo del agente se oculta al negocio y se conserva al guardar."""

from __future__ import annotations

from core.agents.modelo_oculto import con_modelo_guardado, sin_modelo

AGENTE = {"contract": "agentspec/3", "description": "x", "model": {"primary": "deepseek-v4-pro", "router": None}}
KERNEL = {
    "contract": "kernelspec/1",
    "routing": {"coprocessor": {"enabled": True, "model": "deepseek-flash"}},
    "modelPolicy": {"highRiskStage": {"model": "deepseek-v4-pro"}},
}


def test_sin_modelo_quita_las_claves_sin_tocar_el_original() -> None:
    assert "model" not in sin_modelo(AGENTE) and AGENTE["model"]["primary"] == "deepseek-v4-pro"
    kernel = sin_modelo(KERNEL)
    assert "modelPolicy" not in kernel and kernel["routing"]["coprocessor"] == {"enabled": True}
    assert KERNEL["routing"]["coprocessor"]["model"] == "deepseek-flash"


def test_contrato_desconocido_o_no_dict_sale_igual() -> None:
    raro = {"contract": "otro/1", "model": "x"}
    assert sin_modelo(raro) is raro and sin_modelo(None) is None


def test_con_modelo_guardado_repone_e_ignora_lo_enviado() -> None:
    enviado = {"contract": "agentspec/3", "description": "nuevo", "model": {"primary": "claude-haiku-4-5"}}
    assert con_modelo_guardado(enviado, AGENTE) == {**enviado, "model": AGENTE["model"]}
    assert con_modelo_guardado(sin_modelo(AGENTE), AGENTE) == AGENTE
    kernel = con_modelo_guardado({**sin_modelo(KERNEL), "routing": {"coprocessor": {"enabled": False}}}, KERNEL)
    assert kernel["modelPolicy"] == KERNEL["modelPolicy"]
    assert kernel["routing"]["coprocessor"] == {"enabled": False, "model": "deepseek-flash"}


def test_sin_clave_guardada_tampoco_se_acepta_la_enviada() -> None:
    sin = {"contract": "agentspec/3", "description": "x"}
    assert "model" not in con_modelo_guardado({**sin, "model": {"primary": "y"}}, sin)
