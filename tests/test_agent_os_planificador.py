"""HU-AOS-15 · planificador del Agent OS del panel del master (puro, sin base).

Orden de banderas de ADR-0112 y ADR-0138: procedimientos y enrutamiento exigen v3; al encender
runtime → procedimientos → ruteo, al apagar el inverso; apagar solo los procedimientos no fuerza
el enrutamiento a `off` (el CHECK de 0094 solo pide v3).
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

LEGACY = BanderasAOS()
V3 = BanderasAOS(runtime="v3")
TODO = BanderasAOS(runtime="v3", procedimientos=True, ruteo="active")


@pytest.mark.parametrize(
    "objetivo",
    [
        BanderasAOS(runtime="legacy", procedimientos=True),
        BanderasAOS(runtime="legacy", ruteo="shadow"),
        BanderasAOS(runtime="legacy", ruteo="active"),
        BanderasAOS(runtime="v4"),
        BanderasAOS(runtime="v3", ruteo="on"),
    ],
)
def test_objetivo_fuera_de_orden_se_rechaza(objetivo: BanderasAOS) -> None:
    with pytest.raises(ObjetivoInvalido):
        planificar(V3, objetivo)


def test_sin_cambios_no_hay_pasos() -> None:
    assert planificar(TODO, TODO) == []
    assert planificar(LEGACY, LEGACY) == []


def test_encender_todo_va_en_orden() -> None:
    assert planificar(LEGACY, TODO) == ["runtime_v3", "procedimientos_on", "ruteo_active"]


def test_apagar_todo_va_al_reves() -> None:
    assert planificar(TODO, LEGACY) == ["ruteo_off", "procedimientos_off", "runtime_legacy"]


def test_apagar_solo_procedimientos_no_toca_el_ruteo() -> None:
    objetivo = BanderasAOS(runtime="v3", procedimientos=False, ruteo="active")
    assert planificar(TODO, objetivo) == ["procedimientos_off"]


def test_mixto_apaga_antes_de_encender() -> None:
    actual = BanderasAOS(runtime="v3", procedimientos=True, ruteo="off")
    objetivo = BanderasAOS(runtime="v3", procedimientos=False, ruteo="shadow")
    assert planificar(actual, objetivo) == ["procedimientos_off", "ruteo_shadow"]


def test_avisos_del_runbook() -> None:
    assert avisos_del_runbook(V3, TODO) == [AVISO_PASS_K, AVISO_SHADOW]
    sombra_sin_motor = BanderasAOS(runtime="v3", ruteo="shadow")
    assert avisos_del_runbook(V3, sombra_sin_motor) == [AVISO_SIN_PROCEDIMIENTOS]
    # Ya en active: no se repite el del runbook.
    assert avisos_del_runbook(TODO, TODO) == []
