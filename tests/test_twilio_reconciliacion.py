"""Reconciliación de entrantes de Twilio (adenda 2 del ADR-0186), sin base. Cubre la lectura de la API (solo
entrantes, URL de la media igual a la del webhook) y la lógica del cron:
- publica lo que falta dentro de la ventana;
- salta lo ya auditado y lo que está fuera de la ventana;
- con Twilio caído no hace nada;
- no toca un sender que no está `connected`.

Incidente 2026-11-19: entrantes que no llegaron por el webhook."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from agentcore_channel_twilio.client import TwilioClient
from agentcore_channel_twilio.messages import list_inbound_messages, list_media

from core.gateway.twilio import reconciliacion

AC = "AC" + "a" * 32
XE = "XE" + "f" * 32
URL_IN = "https://app.example/webhooks/twilio/pub/inbound"
URL_ST = "https://app.example/webhooks/twilio/pub/status"
SENDER = "+573001112233"
AHORA = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


def _sid() -> str:
    return "SM" + uuid.uuid4().hex


def _msg(
    sid: str, hace: timedelta, *, direction: str = "inbound", num_media: int = 0
) -> dict[str, Any]:
    return {
        "sid": sid,
        "direction": direction,
        "from": "whatsapp:+573009998877",
        "to": f"whatsapp:{SENDER}",
        "body": "Confirmar",
        "num_media": str(num_media),
        "date_created": format_datetime(AHORA - hace),
    }


def _transport(
    mensajes: list[dict[str, Any]],
    *,
    caido: bool = False,
    media: list | None = None,
    webhook_in: str = URL_IN + "#rc=3&rp=5xx,ct",
):
    vistos: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        vistos.append(r)
        if caido:
            raise httpx.ConnectError("sin red", request=r)
        if r.url.path == f"/v2/Channels/Senders/{XE}":
            webhook = {
                "callback_url": webhook_in,
                "status_callback_url": URL_ST + "#rc=3&rp=5xx,ct",
            }
            return httpx.Response(
                200,
                json={
                    "sid": XE,
                    "sender_id": f"whatsapp:{SENDER}",
                    "status": "ONLINE",
                    "webhook": webhook,
                },
            )
        if r.url.path.endswith("/Media.json"):
            return httpx.Response(200, json={"media_list": media or []})
        return httpx.Response(200, json={"messages": mensajes})

    return httpx.MockTransport(handler), vistos


async def test_lista_solo_entrantes_y_filtra_por_dia():
    entrante, saliente = _sid(), _sid()
    transport, vistos = _transport(
        [
            _msg(entrante, timedelta(minutes=5)),
            _msg(saliente, timedelta(minutes=5), direction="outbound-api"),
        ]
    )
    lista = await list_inbound_messages(
        TwilioClient(AC, "tok", transport=transport), f"whatsapp:{SENDER}", desde="2026-11-20"
    )
    assert [m.sid for m in lista] == [entrante]
    assert vistos[0].url.params["To"] == f"whatsapp:{SENDER}"
    assert vistos[0].url.params["DateSent>"] == "2026-11-20"


async def test_media_con_la_url_del_webhook():
    mm, me = "MM" + "b" * 32, "ME" + "c" * 32
    uri = f"/2010-04-01/Accounts/{AC}/Messages/{mm}/Media/{me}.json"
    transport, _ = _transport([], media=[{"uri": uri, "content_type": "image/jpeg"}])
    adjuntos = await list_media(TwilioClient(AC, "tok", transport=transport), mm)
    assert adjuntos == [(f"https://api.twilio.com{uri.removesuffix('.json')}", "image/jpeg")]


@dataclass
class _Fila:
    account_sid: str = AC
    sender_e164: str = SENDER
    sender_sid: str = XE
    webhook_inbound_url: str = URL_IN
    webhook_status_url: str = URL_ST
    public_id: str = "pub"
    modo: str = "sender"
    status: str = "connected"


@dataclass
class _Conn:
    active: bool = True


@dataclass
class _C:
    connection: _Conn = field(default_factory=_Conn)
    fila: _Fila = field(default_factory=_Fila)
    creds: dict = field(default_factory=lambda: {"account_sid": AC, "auth_token": "tok"})


@pytest.fixture
def entorno(monkeypatch):
    estado: dict[str, Any] = {"c": _C(), "vistos": set(), "ingeridos": [], "alertas": 0}

    async def _cargar(_t, _c):
        return estado["c"]

    async def _operativo(_t):
        return True

    async def _existentes(_app, _t, sids):
        return {s for s in sids if s in estado["vistos"]}

    async def _ingerir(_t, _c, _fila, params, _pub, *, via=""):
        estado["ingeridos"].append((params, via))
        return "enqueued"

    async def _alerta(*_a, **_k):
        estado["alertas"] += 1

    monkeypatch.setattr(reconciliacion, "_cargar", _cargar)
    monkeypatch.setattr("core.billing.enforcement.is_tenant_operational", _operativo)
    monkeypatch.setattr("core.gateway.webhooks.webhook_events_existentes", _existentes)
    monkeypatch.setattr("core.gateway.twilio.webhook.ingerir_entrante", _ingerir)
    monkeypatch.setattr("core.shared.alerts.send_alert_once", _alerta)
    return estado


async def test_publica_solo_lo_que_falta_dentro_de_la_ventana(entorno):
    falta, ya_llego, muy_nuevo, muy_viejo = _sid(), _sid(), _sid(), _sid()
    entorno["vistos"].add(ya_llego)
    transport, _ = _transport(
        [
            _msg(falta, timedelta(minutes=10)),
            _msg(ya_llego, timedelta(minutes=8)),
            _msg(muy_nuevo, timedelta(seconds=30)),  # el webhook todavía puede llegar
            _msg(muy_viejo, timedelta(hours=2)),  # fuera de la ventana
        ]
    )
    n = await reconciliacion.reconciliar(
        uuid.uuid4(), uuid.uuid4(), transport=transport, ahora=AHORA
    )
    assert n == 1
    ((params, via),) = entorno["ingeridos"]
    assert (params["MessageSid"], params["Body"], params["AccountSid"], via) == (
        falta,
        "Confirmar",
        AC,
        "reconciliacion",
    )
    assert entorno["alertas"] == 1


async def test_entran_del_mas_viejo_al_mas_nuevo(entorno):
    a, b = _sid(), _sid()
    transport, _ = _transport([_msg(b, timedelta(minutes=3)), _msg(a, timedelta(minutes=9))])
    await reconciliacion.reconciliar(uuid.uuid4(), uuid.uuid4(), transport=transport, ahora=AHORA)
    assert [p["MessageSid"] for p, _ in entorno["ingeridos"]] == [a, b]


async def test_con_media_usa_el_subrecurso(entorno):
    mm, me = "MM" + "d" * 32, "ME" + "e" * 32
    uri = f"/2010-04-01/Accounts/{AC}/Messages/{mm}/Media/{me}.json"
    transport, _ = _transport(
        [_msg(mm, timedelta(minutes=5), num_media=1)],
        media=[{"uri": uri, "content_type": "audio/ogg"}],
    )
    await reconciliacion.reconciliar(uuid.uuid4(), uuid.uuid4(), transport=transport, ahora=AHORA)
    ((params, _),) = entorno["ingeridos"]
    assert params["NumMedia"] == "1"
    assert params["MediaUrl0"].endswith(f"/Media/{me}")
    assert params["MediaContentType0"] == "audio/ogg"


async def test_twilio_caido_no_hace_nada(entorno):
    transport, _ = _transport([], caido=True)
    n = await reconciliacion.reconciliar(
        uuid.uuid4(), uuid.uuid4(), transport=transport, ahora=AHORA
    )
    assert n == 0
    assert entorno["ingeridos"] == [] and entorno["alertas"] == 0


@pytest.mark.parametrize(
    "cambio",
    [
        {"fila": _Fila(status="degraded")},  # el número apunta a otro ambiente
        {"fila": _Fila(modo="sandbox")},
        {"connection": _Conn(active=False)},
    ],
)
async def test_no_toca_un_sender_que_no_es_de_este_ambiente(entorno, cambio):
    entorno["c"] = _C(**cambio)
    transport, vistos = _transport([_msg(_sid(), timedelta(minutes=5))])
    n = await reconciliacion.reconciliar(
        uuid.uuid4(), uuid.uuid4(), transport=transport, ahora=AHORA
    )
    assert n == 0 and vistos == []


async def test_no_publica_si_el_numero_ya_apunta_a_otro_ambiente(entorno):
    """El `status` guardado puede decir `connected` hasta 15 min después de que el número se movió de ambiente: la
    reconciliación lo confirma en vivo y, si apunta a otra URL, no le contesta a clientes ajenos."""
    transport, _ = _transport(
        [_msg(_sid(), timedelta(minutes=5))],
        webhook_in="https://test.example/webhooks/twilio/otro/inbound",
    )
    n = await reconciliacion.reconciliar(
        uuid.uuid4(), uuid.uuid4(), transport=transport, ahora=AHORA
    )
    assert n == 0 and entorno["ingeridos"] == [] and entorno["alertas"] == 0


async def test_un_duplicado_no_cuenta_como_recuperado(entorno, monkeypatch):
    async def _dup(*_a, **_k):
        return "duplicate"

    monkeypatch.setattr("core.gateway.twilio.webhook.ingerir_entrante", _dup)
    transport, _ = _transport([_msg(_sid(), timedelta(minutes=5))])
    n = await reconciliacion.reconciliar(
        uuid.uuid4(), uuid.uuid4(), transport=transport, ahora=AHORA
    )
    assert n == 0 and entorno["alertas"] == 0


async def test_sin_pendientes_no_consulta_el_sender(entorno):
    ya = _sid()
    entorno["vistos"].add(ya)
    transport, vistos = _transport([_msg(ya, timedelta(minutes=5))])
    await reconciliacion.reconciliar(uuid.uuid4(), uuid.uuid4(), transport=transport, ahora=AHORA)
    assert not any("/Senders/" in r.url.path for r in vistos)
