"""HU-AOS-15 · planificador del Agent OS del panel del master (puro, sin base).

Orden de banderas de ADR-0112 y ADR-0138: al encender procedimientos → ruteo, al apagar el inverso;
apagar solo los procedimientos no fuerza el enrutamiento a `off`. El runtime ya no es bandera (ADR-0191).
"""

from __future__ import annotations

import pytest

from core.admin.agent_os import (
    AVISO_PASS_K,
    AVISO_SHADOW,
    AVISO_SIN_PROCEDIMIENTOS,
    ObjetivoInvalido,
    avisos_del_runbook,
    planificar,
)
from core.kernel.banderas import BanderasAOS

APAGADAS = BanderasAOS()
TODO = BanderasAOS(procedimientos=True, ruteo="active")


def test_modo_de_ruteo_desconocido_se_rechaza() -> None:
    with pytest.raises(ObjetivoInvalido):
        planificar(APAGADAS, BanderasAOS(ruteo="on"))


def test_sin_cambios_no_hay_pasos() -> None:
    assert planificar(TODO, TODO) == []
    assert planificar(APAGADAS, APAGADAS) == []


def test_encender_todo_va_en_orden() -> None:
    assert planificar(APAGADAS, TODO) == ["procedimientos_on", "ruteo_active"]


def test_apagar_todo_va_al_reves() -> None:
    assert planificar(TODO, APAGADAS) == ["ruteo_off", "procedimientos_off"]


def test_apagar_solo_procedimientos_no_toca_el_ruteo() -> None:
    objetivo = BanderasAOS(procedimientos=False, ruteo="active")
    assert planificar(TODO, objetivo) == ["procedimientos_off"]


def test_mixto_apaga_antes_de_encender() -> None:
    actual = BanderasAOS(procedimientos=True, ruteo="off")
    objetivo = BanderasAOS(procedimientos=False, ruteo="shadow")
    assert planificar(actual, objetivo) == ["procedimientos_off", "ruteo_shadow"]


def test_avisos_del_runbook() -> None:
    assert avisos_del_runbook(APAGADAS, TODO) == [AVISO_PASS_K, AVISO_SHADOW]
    sombra_sin_motor = BanderasAOS(ruteo="shadow")
    assert avisos_del_runbook(APAGADAS, sombra_sin_motor) == [AVISO_SIN_PROCEDIMIENTOS]
    # Ya en active: no se repite el del runbook.
    assert avisos_del_runbook(TODO, TODO) == []
