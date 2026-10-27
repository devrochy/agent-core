"""Recorrido del onboarding obligatorio (HU-SH-10): estados y transiciones, sin base de datos."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from core.primeros_pasos import recorrido as r

AHORA = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
CORTE = date(2026, 11, 1)
NUEVO = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
VIEJO = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def _r(crudo: object, creado: datetime = NUEVO, desde: date | None = CORTE) -> r.Recorrido:
    return r.desde_crudo(crudo, creado_en=creado, desde=desde)


def test_sin_fecha_de_corte_la_funcion_esta_apagada() -> None:
    actual = _r(None, desde=None)
    assert (actual.estado, actual.aplica, actual.obliga, actual.avisa) == (
        "no_aplica",
        False,
        False,
        False,
    )


def test_negocio_anterior_al_corte_no_aplica_y_el_nuevo_queda_pendiente() -> None:
    assert _r(None, creado=VIEJO).estado == "no_aplica"
    nuevo = _r(None)
    assert (nuevo.estado, nuevo.aplica, nuevo.obliga, nuevo.avisa) == (
        "pendiente",
        True,
        True,
        False,
    )


def test_lo_guardado_manda_aunque_el_negocio_sea_anterior_al_corte() -> None:
    actual = _r({"estado": "omitido", "paso": 3}, creado=VIEJO)
    assert (actual.estado, actual.paso, actual.aplica, actual.avisa, actual.obliga) == (
        "omitido",
        3,
        True,
        True,
        False,
    )


@pytest.mark.parametrize("crudo", [None, [], "x", {"estado": "inventado"}, {"estado": 3}])
def test_datos_mal_formados_se_leen_como_no_guardados(crudo: object) -> None:
    assert _r(crudo).estado == "pendiente"


def test_el_paso_guardado_se_acota() -> None:
    assert _r({"estado": "en_curso", "paso": 99}).paso == r.TOTAL_PASOS
    assert _r({"estado": "en_curso", "paso": -4}).paso == 1


def test_avanzar_empieza_y_retoma() -> None:
    assert r.cambios_de_avanzar(_r(None), 2) == {"estado": "en_curso", "paso": 2}
    assert r.cambios_de_avanzar(_r({"estado": "omitido", "paso": 2}), 3)["estado"] == "en_curso"


@pytest.mark.parametrize("paso", [0, r.TOTAL_PASOS + 1])
def test_avanzar_fuera_de_rango_se_rechaza(paso: int) -> None:
    with pytest.raises(r.TransicionInvalida):
        r.cambios_de_avanzar(_r(None), paso)


def test_no_se_avanza_ni_se_omite_lo_que_no_aplica_o_ya_esta_completo() -> None:
    for actual in (_r(None, creado=VIEJO), _r({"estado": "completo"})):
        with pytest.raises(r.TransicionInvalida):
            r.cambios_de_avanzar(actual, 1)
        with pytest.raises(r.TransicionInvalida):
            r.cambios_de_omitir(actual, por="a", ahora=AHORA)


def test_omitir_guarda_quien_y_cuando_y_conserva_el_paso() -> None:
    cambios = r.cambios_de_omitir(
        _r({"estado": "en_curso", "paso": 4}), por="dueño@x.co", ahora=AHORA
    )
    assert cambios == {
        "estado": "omitido",
        "paso": 4,
        "omitido_en": AHORA.isoformat(),
        "omitido_por": "dueño@x.co",
    }


def test_reanudar_solo_si_estaba_omitido() -> None:
    assert r.cambios_de_reanudar(_r({"estado": "omitido", "paso": 5})) == {
        "estado": "en_curso",
        "paso": 5,
    }
    with pytest.raises(r.TransicionInvalida):
        r.cambios_de_reanudar(_r({"estado": "en_curso"}))


def test_finalizar_exige_negocio_agente_publicado_y_prueba() -> None:
    vacio = _r({"estado": "en_curso", "paso": 6})
    faltan = r.claves_que_faltan(vacio, datos_negocio_completo=False, prueba_hecha=False)
    assert faltan == ["datos_negocio", "agente", "agente_publicado", "prueba"]
    with pytest.raises(r.FaltanPasos) as exc:
        r.cambios_de_finalizar(vacio, faltan, ahora=AHORA)
    assert exc.value.faltan == faltan


def test_finalizar_con_todo_completa_y_la_prueba_la_marca_el_playground() -> None:
    listo = _r(
        {
            "estado": "en_curso",
            "paso": 6,
            "agente_id": "a1",
            "conexion_id": "c1",
            "probado_en": AHORA.isoformat(),
        }
    )
    faltan = r.claves_que_faltan(listo, datos_negocio_completo=True, prueba_hecha=False)
    assert faltan == []
    assert r.cambios_de_finalizar(listo, faltan, ahora=AHORA) == {
        "estado": "completo",
        "paso": r.TOTAL_PASOS,
        "completado_en": AHORA.isoformat(),
    }
