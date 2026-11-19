"""Candidatos de teléfono del enlace Cliente ↔ Customer de cobranza (HU-CRM-15), sin BD."""

from __future__ import annotations

from pack_cobranza.enlazar import candidatos, telefono_para_guardar


def test_local_y_wa_id_comparten_candidatos() -> None:
    # El wa_id de WhatsApp (sin «+») y el número local tecleado a mano deben cruzar.
    assert candidatos("573001234567") & candidatos("300 123-4567")
    assert candidatos("+57 300 123 4567") & candidatos("573001234567")


def test_local_solo_cruza_si_el_prefijo_es_del_pais_por_defecto() -> None:
    # Un wa_id de EE. UU. con los mismos dígitos finales NO es el local colombiano.
    assert not (candidatos("13001234567") & candidatos("3001234567"))
    # Con otro país por defecto el local se interpreta con ese prefijo.
    assert candidatos("3001234567", "MX") & candidatos("523001234567", "MX")


def test_sin_digitos_suficientes_no_hay_candidatos() -> None:
    assert candidatos(None) == set()
    assert candidatos("ext. 12") == set()


def test_telefono_para_guardar_normaliza_o_deja_el_texto() -> None:
    assert telefono_para_guardar(" 300 123 4567 ") == "+573001234567"
    assert telefono_para_guardar("ext 12") == "ext 12"
    assert telefono_para_guardar("   ") is None
