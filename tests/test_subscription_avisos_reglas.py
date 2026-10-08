"""HU-SUB-05: qué recordatorio toca, claves de aviso y config."""

from __future__ import annotations

from datetime import date

import pytest
from pack_subscriptions.avisos import recordatorio_de_hoy
from pack_subscriptions.config import SubscriptionConfig
from pydantic import ValidationError

from core.events.core_handlers import _dias_legibles, notice_key_for

DIAS = [-3, 0, 3, 7]
VENCE = date(2026, 11, 15)
CREADO = date(2026, 11, 12)  # generado 3 días antes (dias_anticipacion)


def test_antes_y_el_dia_ya_los_cubre_el_aviso_del_cargo() -> None:
    assert recordatorio_de_hoy(VENCE, CREADO, date(2026, 11, 12), DIAS) is None  # -3: el aviso del cargo
    assert recordatorio_de_hoy(VENCE, CREADO, date(2026, 11, 15), DIAS) == 0


def test_despues_del_vencimiento() -> None:
    assert recordatorio_de_hoy(VENCE, CREADO, date(2026, 11, 18), DIAS) == 3
    assert recordatorio_de_hoy(VENCE, CREADO, date(2026, 11, 22), DIAS) == 7


def test_si_el_cron_estuvo_caido_solo_el_ultimo() -> None:
    assert recordatorio_de_hoy(VENCE, CREADO, date(2026, 11, 30), DIAS) == 7


def test_antes_del_primer_dia_no_hay_nada() -> None:
    assert recordatorio_de_hoy(VENCE, CREADO, date(2026, 11, 10), DIAS) is None


def test_cargo_creado_tarde_no_repite_el_recordatorio_del_dia() -> None:
    assert recordatorio_de_hoy(VENCE, VENCE, date(2026, 11, 15), DIAS) is None


def test_claves_de_aviso_estables() -> None:
    assert notice_key_for("subscription.charge_created", {"charge_id": "c1"}) == "sub_charge:c1"
    assert notice_key_for("subscription.charge_reminder", {"charge_id": "c1", "dias": 3}) == "sub_reminder:c1:3"
    assert notice_key_for("subscription.paused", {"charge_id": "c1", "subscription_id": "s1"}) == "sub_paused:s1:c1"


def test_dias_legibles() -> None:
    assert [_dias_legibles(d) for d in (-3, 0, 3, "x")] == ["en 3 días", "hoy", "hace 3 días", None]


def test_config_de_avisos() -> None:
    c = SubscriptionConfig()
    assert c.recordatorios_dias == [-3, 0, 3, 7] and c.pausar_tras_dias_mora == 0 and c.dias_anticipacion == 3
    assert SubscriptionConfig(recordatorios_dias=[7, 0, 0, -3]).recordatorios_dias == [-3, 0, 7]
    with pytest.raises(ValidationError):
        SubscriptionConfig(recordatorios_dias=[200])
    for texto in (c.aviso_cargo_texto, c.recordatorio_texto, c.aviso_pausa_texto):
        assert "{cargo.link}" in texto
