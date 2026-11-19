"""Recorrido del onboarding obligatorio (HU-SH-10, HU-UX-23, HU-AOS-84): estados y transiciones, sin base de datos."""

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


def _9(**datos: object) -> dict[str, object]:
    """Un recorrido guardado ya con los 5 pasos de la ruta de lanzamiento (HU-AOS-84)."""
    return {**datos, "pasos": r.TOTAL_PASOS}


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
    actual = _r(_9(estado="omitido", paso=3), creado=VIEJO)
    assert (actual.estado, actual.paso, actual.aplica, actual.avisa, actual.obliga) == (
        "omitido",
        3,
        True,
        True,
        False,
    )


def test_con_un_agente_propio_no_abre_ni_avisa() -> None:
    pendiente = r.desde_crudo(None, creado_en=NUEVO, desde=CORTE, con_agente=True)
    assert (pendiente.estado, pendiente.aplica, pendiente.obliga, pendiente.avisa) == (
        "pendiente",
        True,
        False,
        False,
    )
    for crudo in (_9(estado="en_curso", paso=2), _9(estado="omitido", paso=3)):
        actual = r.desde_crudo(crudo, creado_en=NUEVO, desde=CORTE, con_agente=True)
        assert actual.aplica and not actual.avisa and not actual.obliga


@pytest.mark.parametrize("crudo", [None, [], "x", {"estado": "inventado"}, {"estado": 3}])
def test_datos_mal_formados_se_leen_como_no_guardados(crudo: object) -> None:
    assert _r(crudo).estado == "pendiente"


def test_el_paso_guardado_se_acota() -> None:
    assert _r(_9(estado="en_curso", paso=99)).paso == r.TOTAL_PASOS
    assert _r(_9(estado="en_curso", paso=-4)).paso == 1


def test_lo_guardado_con_5_pasos_se_lee_tal_cual() -> None:
    for paso in range(1, 6):
        assert _r(_9(estado="en_curso", paso=paso)).paso == paso
    assert r.paso_de_5(4, 5) == 4


def test_un_recorrido_de_4_pasos_se_lee_en_su_equivalente_de_5() -> None:
    """HU-AOS-84: la creación rápida de 4 pasa a 5 con «Datos del cliente» en el 3: 1→1, 2→2, 3→4, 4→5."""
    for viejo, nuevo in {1: 1, 2: 2, 3: 4, 4: 5}.items():
        assert _r({"estado": "en_curso", "paso": viejo, "pasos": 4}).paso == nuevo
        assert r.paso_de_5(viejo, 4) == nuevo


def test_el_paso_5_de_4_que_escribia_dejar_listo_es_habilidades() -> None:
    """El `dejar_listo` anterior escribía `paso=5` (de 9) y `fijar` le ponía `pasos: 4`: es el 2 de 5, no
    «Revisar y publicar»."""
    assert _r({"estado": "en_curso", "paso": 5, "pasos": 4}).paso == 2
    assert _r({"estado": "omitido", "paso": 5, "pasos": 4}).paso == 2


def test_los_recorridos_de_6_y_9_pasos_se_leen_en_su_equivalente_de_5() -> None:
    """Lo guardado sin `pasos` es del asistente de 6 (la APK) y lo guardado con 9 es de SPEC-47: pasan por las
    tablas de 6 a 9 y de 9 a 4, y de ahí a 5. Los pasos del negocio (1 a 4 de 9) son el 1."""
    de_6 = {1: 1, 2: 1, 3: 1, 4: 1, 5: 4, 6: 5}
    for viejo, nuevo in de_6.items():
        assert _r({"estado": "en_curso", "paso": viejo}).paso == nuevo
    de_9 = {1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 2, 7: 4, 8: 5, 9: 5}
    for viejo, nuevo in de_9.items():
        assert _r({"estado": "en_curso", "paso": viejo, "pasos": 9}).paso == nuevo
    # Un recorrido completo queda en el último paso, y un paso que no es entero se lee como el primero.
    assert _r({"estado": "completo", "paso": 6}).paso == r.TOTAL_PASOS
    assert r.paso_guardado({"estado": "en_curso", "paso": True}) == 1
    # La escala de 4 de HU-AOS-81 sigue igual por debajo.
    assert r.paso_de_4(9, 9) == 4 and r.paso_de_4(2, 4) == 2 and r.paso_de_4(5, None) == 3


@pytest.mark.parametrize(
    "crudo",
    [
        {"paso": 99, "pasos": 5},
        {"paso": -4, "pasos": 5},
        {"paso": 99, "pasos": 4},
        {"paso": 0, "pasos": 4},
        {"paso": 99, "pasos": 9},
        {"paso": 99},
        {"paso": 3, "pasos": 7},
        {"paso": 3, "pasos": True},
    ],
)
def test_cualquier_escala_queda_entre_1_y_5(crudo: dict[str, object]) -> None:
    assert 1 <= _r({"estado": "en_curso", **crudo}).paso <= r.TOTAL_PASOS


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
        _r(_9(estado="en_curso", paso=4)), por="dueño@x.co", ahora=AHORA
    )
    assert cambios == {
        "estado": "omitido",
        "paso": 4,
        "omitido_en": AHORA.isoformat(),
        "omitido_por": "dueño@x.co",
    }


def test_reanudar_solo_si_estaba_omitido() -> None:
    assert r.cambios_de_reanudar(_r(_9(estado="omitido", paso=3))) == {
        "estado": "en_curso",
        "paso": 3,
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


def test_la_bienvenida_tiene_5_pasos_y_dejar_listo_lleva_a_habilidades() -> None:
    assert (r.TOTAL_PASOS, r.PASO_DE_DEJAR_LISTO) == (5, 2)
