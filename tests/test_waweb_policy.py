"""Tests puros de la política anti-baneo del canal `waweb` (ADR-0074)."""

from __future__ import annotations

from core.waweb.policy import (
    DEFAULT_DAILY_OUTBOUND_CAP,
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_MIN_DELAY_MS,
    MODE_ALLOW_OUTBOUND,
    MODE_REPLY_ONLY,
    WaWebPolicy,
    policy_from_behavior,
)


def test_defaults_son_solo_respuesta() -> None:
    p = policy_from_behavior(None)
    assert p.mode == MODE_REPLY_ONLY
    assert p.allows_outbound is False
    assert p.send_min_delay_ms == DEFAULT_MIN_DELAY_MS
    assert p.send_max_delay_ms == DEFAULT_MAX_DELAY_MS
    assert p.daily_outbound_cap == DEFAULT_DAILY_OUTBOUND_CAP


def test_modo_invalido_cae_a_solo_respuesta() -> None:
    assert policy_from_behavior({"waweb_mode": "cualquier_cosa"}).mode == MODE_REPLY_ONLY
    assert policy_from_behavior({"waweb_mode": None}).mode == MODE_REPLY_ONLY


def test_modo_outbound_explicito_se_respeta() -> None:
    p = policy_from_behavior({"waweb_mode": MODE_ALLOW_OUTBOUND})
    assert p.allows_outbound is True


def test_clamps_de_delays_y_cap() -> None:
    p = policy_from_behavior(
        {
            "waweb_send_min_delay_ms": 999_999,
            "waweb_send_max_delay_ms": 999_999,
            "waweb_daily_outbound_cap": -5,
        }
    )
    assert p.send_min_delay_ms == 60_000
    assert p.send_max_delay_ms == 120_000
    assert p.daily_outbound_cap == 0


def test_max_menor_que_min_se_iguala() -> None:
    p = policy_from_behavior({"waweb_send_min_delay_ms": 3000, "waweb_send_max_delay_ms": 1000})
    assert p.send_max_delay_ms == 3000


def test_valores_no_numericos_caen_al_default() -> None:
    p = policy_from_behavior({"waweb_send_min_delay_ms": "abc", "waweb_daily_outbound_cap": None})
    assert p.send_min_delay_ms == DEFAULT_MIN_DELAY_MS
    assert p.daily_outbound_cap == DEFAULT_DAILY_OUTBOUND_CAP


def test_warmup_bool_tolerante() -> None:
    assert policy_from_behavior({"waweb_warmup_enabled": False}).warmup_enabled is False
    assert policy_from_behavior({"waweb_warmup_enabled": "off"}).warmup_enabled is False
    assert policy_from_behavior({"waweb_warmup_enabled": "sí"}).warmup_enabled is True


def test_behavior_no_dict_no_rompe() -> None:
    assert policy_from_behavior("no soy dict") == WaWebPolicy()  # type: ignore[arg-type]
