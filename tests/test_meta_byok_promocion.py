"""`puede_terminar`: la puerta de "Pasar a producción" (HU-CONN-06, ADR-0127)."""

from __future__ import annotations

from core.gateway.meta_byok_promocion import CHECKS_OBLIGATORIOS, puede_terminar
from core.gateway.meta_preflight import PreflightCheck


def _checks(**oks) -> list[PreflightCheck]:
    base = dict.fromkeys(CHECKS_OBLIGATORIOS, True) | {"templates": True, "app_subscription_active": None}
    base.update(oks)
    return [PreflightCheck(key=k, ok=v, detail="") for k, v in base.items()]


def test_todo_en_verde_pasa():
    assert puede_terminar(_checks()) is True


def test_un_none_en_uno_opcional_no_bloquea():
    assert puede_terminar(_checks(app_subscription_active=None)) is True


def test_un_none_en_uno_obligatorio_bloquea():
    # Meta no respondió el `debug_token`: no se promueve sin saber si el token sirve.
    assert puede_terminar(_checks(token_valid=None)) is False


def test_cualquier_falso_bloquea():
    assert puede_terminar(_checks(templates=False)) is False


def test_falta_un_obligatorio_bloquea():
    checks = [c for c in _checks() if c.key != "waba_subscribed_to_own_app"]
    assert puede_terminar(checks) is False
