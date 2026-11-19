"""PR-5 de WhatsApp vía Twilio (ADR-0186 §10-11), sin base: precio del recurso Message, restaurar webhooks y la tarea
de costos (reintenta sin precio o con Twilio caído; se rinde a los 3 intentos)."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from agentcore_channel_twilio.client import TwilioClient, require_sid
from agentcore_channel_twilio.messages import get_message_price
from agentcore_channel_twilio.senders import restore_webhooks

from core.gateway.channels.base import ProviderError

AC = "AC" + "a" * 32
XE = "XE" + "b" * 32
SM = "SM" + "c" * 32


def _cliente(handler) -> tuple[TwilioClient, list[httpx.Request]]:
    vistos: list[httpx.Request] = []

    def _wrap(r):
        vistos.append(r)
        return handler(r)

    return TwilioClient(AC, "tok", transport=httpx.MockTransport(_wrap)), vistos


@pytest.mark.parametrize(
    ("price", "esperado"),
    [
        ("-0.00500", Decimal("0.00500")),
        (None, None),
        ("", None),
        ("abc", None),
        ("0.01", Decimal("0.01")),
    ],
)
async def test_precio_del_mensaje(price, esperado):
    client, vistos = _cliente(
        lambda r: httpx.Response(
            200, json={"sid": SM, "status": "Delivered", "price": price, "price_unit": "USD"}
        )
    )
    precio = await get_message_price(client, SM)
    assert (precio.price, precio.price_unit, precio.status) == (esperado, "usd", "delivered")
    assert (
        str(vistos[0].url) == f"https://api.twilio.com/2010-04-01/Accounts/{AC}/Messages/{SM}.json"
    )


def test_sid_de_mensaje():
    assert require_sid(SM, "SM") == SM
    assert require_sid("MM" + "d" * 32, "SM").startswith("MM")
    with pytest.raises(ProviderError):
        require_sid("SMcorto", "SM")


async def test_restaurar_webhooks_vacia_lo_que_no_estaba():
    client, vistos = _cliente(
        lambda r: httpx.Response(
            200, json={"sid": XE, "sender_id": "whatsapp:+1", "status": "ONLINE"}
        )
    )
    await restore_webhooks(client, XE, {"callback_url": "https://otra.app/in"})
    assert json.loads(vistos[0].content) == {
        "webhook": {
            "callback_url": "https://otra.app/in",
            "callback_method": "POST",
            "status_callback_url": "",
            "status_callback_method": "POST",
            "fallback_url": "",
            "fallback_method": "POST",
        }
    }


@pytest.mark.parametrize(
    ("resultado", "reintenta"),
    [
        ("guardado", False),
        ("ya_estaba", False),
        ("sin_conexion", False),
        ("error", False),
        ("sin_precio", True),
        ("error_transitorio", True),
    ],
)
def test_tarea_de_costos_reintenta_solo_lo_pasajero(monkeypatch, resultado, reintenta):
    from core.tasks import twilio_costos

    async def _registrar(*_a, **_k):
        return resultado

    monkeypatch.setattr("core.gateway.twilio.costos.registrar_costo", _registrar)
    monkeypatch.setattr(twilio_costos, "run_async", lambda coro: (coro.close(), resultado)[1])
    pedidos: list[int] = []

    class _Reintento(Exception):
        pass

    def _retry(**kw):
        pedidos.append(kw["countdown"])
        return _Reintento()

    monkeypatch.setattr(twilio_costos.costo_mensaje, "retry", _retry)
    tid, cid = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    if reintenta:
        with pytest.raises(_Reintento):
            twilio_costos.costo_mensaje.run(tid, cid, SM)
        assert pedidos == [300]
    else:
        assert twilio_costos.costo_mensaje.run(tid, cid, SM) == resultado
        assert pedidos == []
