"""SPEC-37: cada guardrail del catálogo trae su nombre legible y qué evita (panel y app los muestran)."""

from __future__ import annotations

import pytest

from core.config_registro import catalogo_para
from core.config_registro.catalogo import INFO_VERIFICADOR
from core.mcp.host import MCPHost
from core.orchestrator.verificador.verificador import TOGGLES


async def _ok(texto, resultados):  # pragma: no cover - solo se registra
    raise NotImplementedError


def _valores(catalogo):
    opcion = next(o for o in catalogo.opciones if o.clave == "disabled_guardrails")
    return {v.valor: v for v in opcion.valores}


def test_los_interruptores_del_verificador_traen_etiqueta_y_explicacion():
    assert set(INFO_VERIFICADOR) == set(TOGGLES)
    valores = _valores(catalogo_para(es_master=False, guardrails_disponibles=list(TOGGLES)))
    for clave in TOGGLES:
        assert valores[clave].etiqueta != clave
        assert valores[clave].nota.startswith(("Evita", "Quita"))


def test_el_guardrail_de_un_pack_usa_su_texto_y_sin_texto_cae_al_nombre():
    info = {"cob_promesa_check": ("Promesas de pago", "Evita decir que registró una promesa.")}
    valores = _valores(
        catalogo_para(
            es_master=False,
            guardrails_disponibles=["cob_promesa_check", "otro_check"],
            guardrails_info=info,
        )
    )
    assert valores["cob_promesa_check"].etiqueta == "Promesas de pago"
    assert valores["cob_promesa_check"].nota == "Evita decir que registró una promesa."
    assert valores["otro_check"].etiqueta == "otro_check"
    assert valores["otro_check"].nota == ""


def test_el_host_guarda_el_texto_que_declara_el_pack():
    host = MCPHost()
    host._active_plugin = "pack_x"
    host.register_guardrail("x_check", _ok, etiqueta="Revisión X", explicacion="Evita X.")
    guardrail = host.get_guardrails()[0]
    assert (guardrail.etiqueta, guardrail.explicacion) == ("Revisión X", "Evita X.")


@pytest.mark.parametrize("nombre", ["cob_promesa_check", "cob_suscripcion_check", "calendar_completion_check", "subs_completion_check"])
def test_los_guardrails_de_los_packs_declaran_su_texto(nombre):
    import pathlib
    import re

    raiz = pathlib.Path(__file__).resolve().parent.parent / "packages"
    codigo = "\n".join(p.read_text() for p in raiz.glob("pack-*/*/module.py"))
    bloque = re.search(rf'register_guardrail\(\s*"{nombre}".*?\n\s*\)', codigo, re.S)
    assert bloque and "etiqueta=" in bloque.group(0) and "explicacion=" in bloque.group(0)
