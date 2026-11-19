"""HU-AOS-59 · Quitar una acción o un procedimiento del binario exige una entrada en el registro de
retiros (ADR-0184).

Compara el binario de hoy con la instantánea commiteada (`packages/catalogo-nombres.lock.json`,
`python scripts/catalogo_nombres_lock.py`): si desaparece un nombre sin entrada en
`core/agents/retiros.py`, los agentes y las releases que lo nombran quedarían rotos sin aviso.
"""

from __future__ import annotations

from core.agents.referencias_retiradas import binario_de, nombres_del_binario
from core.agents.retiros import (
    RETIROS,
    Reemplazo,
    Retiro,
    desaparecidos,
    leer_lock,
    problemas_del_lock,
    retiro_de,
)
from core.releases.procedimientos import cumple


def _modulos() -> list[object]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


def test_el_binario_coincide_con_la_instantanea_y_el_registro() -> None:
    problemas = problemas_del_lock(leer_lock(), nombres_del_binario(_modulos()))
    assert not problemas, "\n".join(problemas)


def test_cada_reemplazo_del_registro_lo_publica_el_binario_en_esa_version() -> None:
    binario = binario_de(_modulos())
    for retiro in RETIROS:
        if retiro.reemplazo is None:
            continue
        publicados = binario.procedimientos.get(retiro.reemplazo.id, ())
        assert any(cumple(p.version, retiro.reemplazo.version) for p in publicados), (
            f"{retiro.nombre}: ninguna versión publicada de {retiro.reemplazo.id} cumple {retiro.reemplazo.version}"
        )


def test_el_registro_trae_los_dos_retiros_de_spec_43() -> None:
    plan = retiro_de("procedimiento", "pack_commerce.contratar_plan")
    assert plan is not None and plan.reemplazo == Reemplazo("pack_subscriptions.contratar_plan", "^2.0")
    informar = retiro_de("accion", "informar_plan")
    assert informar is not None and informar.reemplazo is None and informar.destino == "quitar"
    assert retiro_de("accion", "otra_cosa") is None


def test_un_nombre_que_desaparece_sin_entrada_falla_el_ci() -> None:
    lock = {"acciones": ["a", "b"], "procedimientos": ["pack_x.p"]}
    actual = {"acciones": ["a"], "procedimientos": []}
    assert desaparecidos(lock, actual) == [("accion", "b"), ("procedimiento", "pack_x.p")]
    problemas = problemas_del_lock(lock, actual, retiros=())
    assert len(problemas) == 2 and all("no tiene entrada en el registro de retiros" in p for p in problemas)


def test_con_entrada_en_el_registro_el_retiro_pasa() -> None:
    lock = {"acciones": ["a", "b"], "procedimientos": ["pack_x.p", "pack_y.q"]}
    actual = {"acciones": ["a"], "procedimientos": ["pack_y.q"]}
    registro = (
        Retiro("accion", "b", "2026-01-01", "prueba"),
        Retiro("procedimiento", "pack_x.p", "2026-01-01", "prueba", Reemplazo("pack_y.q", "^1.0")),
    )
    assert problemas_del_lock(lock, actual, retiros=registro) == []


def test_un_nombre_nuevo_hay_que_pasarlo_por_el_lock() -> None:
    problemas = problemas_del_lock({"acciones": ["a"]}, {"acciones": ["a", "nueva"]}, retiros=())
    assert problemas and "catalogo_nombres_lock.py" in problemas[0]


def test_una_entrada_vieja_que_el_binario_todavia_trae_o_con_reemplazo_inexistente_se_marca() -> None:
    actual = {"acciones": ["a"], "procedimientos": ["pack_y.q"]}
    viva = (Retiro("accion", "a", "2026-01-01", "prueba"),)
    assert any("todavía lo trae" in p for p in problemas_del_lock(actual, actual, retiros=viva))
    huerfano = (Retiro("procedimiento", "pack_x.p", "2026-01-01", "prueba", Reemplazo("pack_z.nada", "^1.0")),)
    assert any("no existe en el binario" in p for p in problemas_del_lock(actual, actual, retiros=huerfano))


def test_el_registro_se_valida_al_construirlo() -> None:
    import pytest

    with pytest.raises(ValueError, match="solo un procedimiento"):
        Retiro("accion", "informar_plan", "2026-01-01", "x", Reemplazo("pack_y.q", "^1.0"))
    with pytest.raises(ValueError, match="nombre inválido"):
        Retiro("procedimiento", "sin_punto", "2026-01-01", "x")
    with pytest.raises(ValueError, match="rango"):
        Reemplazo("pack_y.q", "latest")
    with pytest.raises(ValueError, match="repetida"):
        from core.agents.retiros import _indice

        _indice((Retiro("accion", "a", "2026-01-01", "x"), Retiro("accion", "a", "2026-01-02", "y")))
