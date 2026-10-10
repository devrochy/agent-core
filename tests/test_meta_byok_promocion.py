"""`puede_terminar`: la puerta de "Pasar a producción" (HU-CONN-06, ADR-0127) sobre el checklist
por conexión de HU-CONN-07."""

from __future__ import annotations

from core.gateway.meta_byok_promocion import CHECKS_OBLIGATORIOS, puede_terminar
from core.gateway.meta_preflight import PreflightCheck
from core.gateway.meta_production_readiness import CONNECTION_ITEM_LABELS


def _checks(**oks) -> list[PreflightCheck]:
    base = dict.fromkeys(CHECKS_OBLIGATORIOS, True) | {"webhook": None}
    base.update(oks)
    return [PreflightCheck(key=k, ok=v, detail="") for k, v in base.items()]


def test_los_obligatorios_son_items_del_checklist_mas_los_de_la_promocion():
    # La promoción no tiene chequeos propios con otras claves: usa las del checklist por conexión.
    assert set(CHECKS_OBLIGATORIOS) - set(CONNECTION_ITEM_LABELS) == {"own_app", "real_number"}


def test_todo_en_verde_pasa():
    assert puede_terminar(_checks()) is True


def test_un_none_en_uno_opcional_no_bloquea():
    # El webhook sin dato: el cron de 15 min todavía no leyó la suscripción de la app.
    assert puede_terminar(_checks(webhook=None)) is True


def test_un_none_en_uno_obligatorio_bloquea():
    # Meta no respondió el `debug_token`: no se promueve sin saber si el token sirve.
    assert puede_terminar(_checks(token=None)) is False


def test_cualquier_falso_bloquea():
    assert puede_terminar(_checks(webhook=False)) is False
    assert puede_terminar(_checks(templates_approved=False)) is False


def test_falta_un_obligatorio_bloquea():
    checks = [c for c in _checks() if c.key != "waba_subscribed"]
    assert puede_terminar(checks) is False


def test_los_chequeos_tienen_tope_de_tiempo(monkeypatch):
    """Corren en el request: si Meta no responde, 504 claro en vez de un request colgado."""
    import asyncio
    from types import SimpleNamespace

    import pytest

    import core.gateway.meta_byok_promocion as promo

    async def _lento(_connection):
        await asyncio.sleep(1)
        return []

    monkeypatch.setattr(promo, "checks_de_produccion", _lento)
    monkeypatch.setattr(promo, "TOPE_CHEQUEOS_S", 0.01)

    with pytest.raises(promo.PromocionError) as exc:
        asyncio.run(promo._chequear_con_tope(SimpleNamespace(tenant_id="t", id="c")))
    assert exc.value.status == 504 and exc.value.code == "checks_timeout"
