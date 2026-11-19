"""Datos de prueba (HU-QA-07): fragmentos `qa.json` de los packs y resumen del contenido (sin BD)."""

from __future__ import annotations

import uuid

from core.presets import load_fragment
from core.qa import datos


def test_fragmentos_qa_de_los_tres_packs():
    for pack in datos.PACKS:
        assert load_fragment(pack, "qa").seed, pack


def test_correos_y_slug_por_tester_en_dominio_reservado():
    tester = uuid.UUID("afe5d7cc-1234-4000-8000-000000000000")
    assert datos.slug_de(tester) == "qa-afe5d7cc1234"
    assert datos.correo_de("gestor", tester) == "gestor.afe5d7cc1234@pruebas.solvershub.test"


def test_contenido_describe_clientes_en_mora_al_dia_y_sin_prestamo():
    contenido = {c["habilidad"]: c["detalle"] for c in datos.contenido()}
    assert set(contenido) == {"Comercio", "Agenda", "Cobranza"}
    cobranza = " | ".join(contenido["Cobranza"])
    assert "Juana Pérez · en mora (9 cuotas atrasadas)" in cobranza
    assert "Carlos Ruiz · al día" in cobranza
    assert "Ana Gómez · sin préstamo" in cobranza
    assert any("(agotado)" in linea for linea in contenido["Comercio"])
