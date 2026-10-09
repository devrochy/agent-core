"""`payment.confirmed` lleva el monto que Wompi firmó, no el que pedimos.

Antes `_apply_webhook_event` emitía `amount = transaction.amount` (el del link):
la validación de "monto exacto" de los consumidores (p. ej.
`pack_subscriptions::_on_payment_confirmed`) comparaba dos valores del servidor
y dependía de que Wompi respetara el monto fijo del link. Ahora el monto del
webhook (ya verificado por firma) se compara acá: si difiere, no se confirma,
se audita y se avisa a los dueños.

La sesión es falsa: lo que se prueba es la decisión (confirmar o no, qué se
emite, cuándo se alerta), no SQLAlchemy.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pack_payments.module as module_mod
import pytest
from pack_payments.module import MONTO_DISTINTO, _apply_webhook_event, _monto_coincide
from pack_payments.providers.base import WebhookEvent

TENANT = uuid.UUID("10000000-0000-0000-0000-0000000000a1")


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeSession:
    def __init__(self, transaction):
        self.transaction = transaction

    async def execute(self, _stmt):
        return _Result(self.transaction)

    async def flush(self):
        return None

    async def refresh(self, _obj):
        return None


@pytest.fixture
def entorno(monkeypatch):
    """Transacción pendiente de $150.000 y captura de eventos, auditoría y push."""
    transaction = SimpleNamespace(
        id=uuid.uuid4(),
        reference="sub-1",
        provider_reference="link-abc",
        status="pendiente",
        amount=Decimal("150000.00"),
        last_webhook_payload=None,
    )
    emitted: list[dict] = []
    audits: list[dict] = []
    pushes: list[tuple] = []

    @asynccontextmanager
    async def _get_session():
        yield _FakeSession(transaction)

    async def _emit_domain(_session, *, tenant_id, topic, dedupe_key, payload):
        emitted.append({"topic": topic, "dedupe_key": dedupe_key, "payload": payload})

    async def _record_audit(**kwargs):
        audits.append(kwargs)

    import core.events.outbox as outbox_mod
    import core.shared.audit as audit_mod
    import core.tasks.push_tasks as push_mod

    monkeypatch.setattr(module_mod, "get_session", _get_session)
    monkeypatch.setattr(module_mod, "get_current_tenant_id", lambda: TENANT)
    monkeypatch.setattr(outbox_mod, "emit_domain", _emit_domain)
    monkeypatch.setattr(audit_mod, "record_audit", _record_audit)
    monkeypatch.setattr(push_mod.notify_owners_push, "delay", lambda *a: pushes.append(a))
    return SimpleNamespace(transaction=transaction, emitted=emitted, audits=audits, pushes=pushes)


def _event(status="aprobado", amount=150_000.0):
    return WebhookEvent(reference="sub-1", provider_reference="link-abc", status=status, raw={}, amount=amount)


@pytest.mark.parametrize(
    ("pagado", "esperado", "coincide"),
    [
        (150_000.0, Decimal("150000.00"), True),
        (150_000.01, Decimal("150000.00"), True),  # tolerancia de un centavo
        (150_000.02, Decimal("150000.00"), False),
        (1_500.0, Decimal("150000.00"), False),
        (None, Decimal("150000.00"), False),  # monto no firmado: nunca coincide
        (150_000.0, None, False),
    ],
)
def test_monto_coincide(pagado, esperado, coincide):
    assert _monto_coincide(pagado, esperado) is coincide


@pytest.mark.asyncio
async def test_aprobado_con_monto_igual_confirma_con_el_monto_del_webhook(entorno):
    await _apply_webhook_event(_event(amount=150_000.0), {"p": 1}, "wompi")

    assert entorno.transaction.status == "aprobado"
    assert len(entorno.emitted) == 1
    assert entorno.emitted[0]["topic"] == "payment.confirmed"
    assert entorno.emitted[0]["payload"]["amount"] == 150_000.0
    assert entorno.audits == [] and entorno.pushes == []


@pytest.mark.asyncio
async def test_aprobado_con_monto_distinto_no_confirma_audita_y_avisa(entorno):
    await _apply_webhook_event(_event(amount=1_500.0), {"p": 1}, "wompi")

    assert entorno.transaction.status == MONTO_DISTINTO
    assert entorno.transaction.last_webhook_payload == {"p": 1}
    assert entorno.emitted == []
    assert len(entorno.audits) == 1
    assert entorno.audits[0]["action"] == "payments_pago_monto_distinto"
    assert entorno.audits[0]["output_data"]["esperado"] == "150000.00"
    assert entorno.audits[0]["output_data"]["recibido"] == "1500.0"
    assert len(entorno.pushes) == 1
    assert entorno.pushes[0][0] == str(TENANT)


@pytest.mark.asyncio
async def test_aprobado_sin_monto_firmado_no_confirma(entorno):
    await _apply_webhook_event(_event(amount=None), {}, "wompi")

    assert entorno.transaction.status == MONTO_DISTINTO
    assert entorno.emitted == []
    assert entorno.audits[0]["output_data"]["recibido"] is None
    assert "sin verificar" in entorno.pushes[0][2]


@pytest.mark.asyncio
async def test_reintento_del_mismo_webhook_no_repite_la_alerta(entorno):
    await _apply_webhook_event(_event(amount=1_500.0), {}, "wompi")
    await _apply_webhook_event(_event(amount=1_500.0), {}, "wompi")

    assert len(entorno.audits) == 1
    assert len(entorno.pushes) == 1
    assert entorno.emitted == []


@pytest.mark.asyncio
async def test_rechazo_posterior_no_saca_la_transaccion_de_revision(entorno):
    await _apply_webhook_event(_event(amount=1_500.0), {}, "wompi")
    await _apply_webhook_event(_event(status="rechazado", amount=1_500.0), {"p": 2}, "wompi")

    assert entorno.transaction.status == MONTO_DISTINTO
    assert entorno.transaction.last_webhook_payload == {"p": 2}
    assert entorno.emitted == []


@pytest.mark.asyncio
async def test_aprobado_correcto_tras_monto_distinto_confirma(entorno):
    """Un intento posterior que sí paga el monto correcto confirma normalmente."""
    await _apply_webhook_event(_event(amount=1_500.0), {}, "wompi")
    await _apply_webhook_event(_event(amount=150_000.0), {}, "wompi")

    assert entorno.transaction.status == "aprobado"
    assert [e["topic"] for e in entorno.emitted] == ["payment.confirmed"]


@pytest.mark.asyncio
async def test_rechazado_sin_monto_firmado_emite_el_monto_del_link(entorno):
    await _apply_webhook_event(_event(status="rechazado", amount=None), {}, "wompi")

    assert entorno.transaction.status == "rechazado"
    assert entorno.emitted[0]["topic"] == "payment.rejected"
    assert entorno.emitted[0]["payload"]["amount"] == 150_000.0
    assert entorno.audits == []


@pytest.mark.asyncio
async def test_reenvio_sin_monto_firmado_no_revierte_un_pago_ya_confirmado(entorno):
    await _apply_webhook_event(_event(amount=150_000.0), {}, "wompi")
    await _apply_webhook_event(_event(amount=None), {"p": 2}, "wompi")

    assert entorno.transaction.status == "aprobado"
    assert entorno.transaction.last_webhook_payload == {"p": 2}
    assert [e["topic"] for e in entorno.emitted] == ["payment.confirmed"]
    assert entorno.audits == [] and entorno.pushes == []
