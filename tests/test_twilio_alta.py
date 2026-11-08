"""Alta de WhatsApp vía Twilio (ADR-0186, PR-3): bandera por negocio, catálogo y reglas del alta sin base.

Lo que necesita Postgres (unicidad del número entre negocios, RLS, idempotencia, ruteo por `public_id`) vive en
`tests/integration/test_twilio_alta_db.py`.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import httpx
import pytest
from agentcore_channel_twilio.client import TwilioClient
from agentcore_channel_twilio.senders import Sender

from core.gateway.channels.base import ProviderError
from core.gateway.connection_types import (
    BY_ID,
    SETUP_WIZARD,
    WHATSAPP_TWILIO,
    disponible_para,
    external_id_for,
    normalize_credentials,
    setup_url,
)
from core.gateway.twilio import alta, bandera

ROOT = Path(__file__).resolve().parents[1]
AC = "AC" + "a" * 32
XE = "XE" + "b" * 32


# --- bandera ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "esperado"),
    [
        ({}, False),
        (None, False),
        ({"canales": {}}, False),
        ({"canales": {"whatsapp_twilio": "true"}}, False),
        ({"canales": {"whatsapp_twilio": 1}}, False),
        ({"canales": ["whatsapp_twilio"]}, False),
        ({"canales": {"whatsapp_twilio": False}}, False),
        ({"canales": {"whatsapp_twilio": True}}, True),
    ],
)
def test_solo_el_literal_true_abre_el_canal(settings, esperado):
    assert bandera.habilitado_desde(settings, "whatsapp_twilio") is esperado


async def test_canal_desconocido():
    with pytest.raises(bandera.CanalDesconocido):
        await bandera.leer(uuid.uuid4(), "telegram")


def test_nadie_fuera_de_la_ruta_master_fija_la_bandera():
    """`bandera.fijar` es el único escritor de `settings.canales` y solo lo llama la ruta master-only (que lo
    importa como `canal_bandera`; `bandera` a secas es el de opciones directas)."""
    llamadas = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        arbol = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(arbol):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fijar"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "canal_bandera"
            ):
                llamadas.append(path.relative_to(ROOT).as_posix())
    assert llamadas == ["core/api/routers/platform.py"]
    textos = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "core").rglob("*.py")
        if "'{canales}'" in p.read_text()
    ]
    assert textos == ["core/gateway/twilio/bandera.py"]


# --- catálogo --------------------------------------------------------------------------------


def test_el_tipo_de_twilio_se_abre_por_negocio():
    tipo = BY_ID[WHATSAPP_TWILIO]
    # Sigue «Próximamente» para todos (y en el panel clásico), salvo con la bandera del negocio.
    assert tipo.available is False
    assert setup_url(tipo) == ""
    assert disponible_para(tipo, {}) is False
    assert disponible_para(tipo, {"canales": {"whatsapp_twilio": True}}) is True
    # Lo arma el alta guiada, no el formulario manual (que exige `setup_kind == manual`).
    assert tipo.setup_kind == SETUP_WIZARD
    # Plivo no tiene bandera: sigue no disponible aunque el negocio tenga la de Twilio.
    assert disponible_para(BY_ID["whatsapp_plivo"], {"canales": {"whatsapp_twilio": True}}) is False
    # Un tipo disponible para todos no depende de la bandera.
    assert disponible_para(BY_ID["whatsapp_meta"], {}) is True


def test_credenciales_y_clave_de_ruteo():
    tipo = BY_ID[WHATSAPP_TWILIO]
    limpias = normalize_credentials(
        tipo,
        {"account_sid": AC, "auth_token": "t", "sender_e164": "+15550001111", "intruso": "x"},
        {},
    )
    assert limpias == {"account_sid": AC, "auth_token": "t", "sender_e164": "+15550001111"}
    assert external_id_for(tipo, limpias) == "+15550001111"
    propiedades = tipo.credentials_schema["properties"]
    assert propiedades["auth_token"]["format"] == "password"
    assert propiedades["auth_token"]["writeOnly"] is True


# --- alta: reglas sin base -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sin_scope(request, monkeypatch):
    """Sin base ni `tenant_scope`: la guarda del scope tiene su propio test (`test_alta_exige_el_scope`)."""
    if request.node.name != "test_alta_exige_el_scope":
        monkeypatch.setattr(alta, "_exigir_scope", lambda tenant_id: None)


async def test_alta_exige_el_scope():
    from core.shared.tenant import tenant_scope

    tid = uuid.uuid4()
    with pytest.raises(PermissionError):
        await alta.descubrir(tid, AC, "tok")
    async with tenant_scope(uuid.uuid4()):
        with pytest.raises(PermissionError):
            await alta.conectar(tid, account_sid=AC, auth_token="tok", sender_sid=XE)


def _sender(**webhook) -> Sender:
    return Sender(sid=XE, sender_id="whatsapp:+15550001111", status="ONLINE", webhook=webhook)


def test_urls_y_webhook_ajeno(monkeypatch):
    monkeypatch.setattr(
        alta, "_base_webhooks", lambda: "https://test.solvershub.site/webhooks/twilio/"
    )
    entrada, estados = alta.urls_webhook("pub123")
    assert entrada == "https://test.solvershub.site/webhooks/twilio/pub123/inbound"
    assert estados == "https://test.solvershub.site/webhooks/twilio/pub123/status"
    assert alta._webhook_ajeno(_sender()) == {}
    # Nuestros (de esta u otra conexión de solvershub) no piden confirmación.
    assert alta._webhook_ajeno(_sender(callback_url=entrada, status_callback_url=estados)) == {}
    ajeno = _sender(
        callback_url="https://otra.app/in", status_callback_url=estados, fallback_url=""
    )
    assert alta._webhook_ajeno(ajeno) == {"callback_url": "https://otra.app/in"}


@pytest.mark.parametrize(
    ("error", "http", "code"),
    [
        (
            ProviderError("x", provider="twilio", http_status=401, code=20003, category="AUTH"),
            422,
            "twilio_credentials_invalid",
        ),
        (
            ProviderError("x", provider="twilio", category="CONFIG"),
            422,
            "twilio_credentials_invalid",
        ),
        (ProviderError("x", provider="twilio", http_status=503), 502, "twilio_unavailable"),
        (ProviderError("x", provider="twilio", category="AMBIGUOUS"), 502, "twilio_unavailable"),
        (
            ProviderError("x", provider="twilio", http_status=404, code=20404),
            422,
            "sender_not_found",
        ),
    ],
)
def test_traduce_los_errores_de_twilio(error, http, code):
    traducido = alta._traducir(error)
    # Nunca 401: para el panel, un 401 es «tu sesión expiró».
    assert (traducido.http_status, traducido.code) == (http, code)


def _twilio(*, cuenta="active", senders=None, webhook=None):
    estado = {"webhook": dict(webhook or {})}
    lista = (
        senders
        if senders is not None
        else [
            {
                "sid": XE,
                "sender_id": "whatsapp:+15550001111",
                "status": "ONLINE",
                "webhook": estado["webhook"],
            }
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/Accounts/{AC}.json"):
            return httpx.Response(
                200, json={"status": cuenta, "friendly_name": "Mi cuenta", "type": "Full"}
            )
        if path == "/v2/Channels/Senders":
            return httpx.Response(200, json={"senders": lista, "meta": {}})
        if path == f"/v2/Channels/Senders/{XE}":
            if request.method == "POST":
                import json

                estado["webhook"] = json.loads(request.content)["webhook"]
            return httpx.Response(200, json={**lista[0], "webhook": estado["webhook"]})
        return httpx.Response(404, json={"code": 20404, "message": "no"})

    return httpx.MockTransport(handler), estado


async def test_descubrir_marca_seleccionables(monkeypatch):
    senders = [
        {
            "sid": XE,
            "sender_id": "whatsapp:+15550001111",
            "status": "ONLINE",
            "webhook": {"callback_url": "https://otra/in"},
        },
        {"sid": "XE" + "c" * 32, "sender_id": "whatsapp:+15550002222", "status": "OFFLINE"},
        {"sid": "XE" + "d" * 32, "sender_id": "whatsapp:+15550003333", "status": "ONLINE"},
    ]
    transport, _ = _twilio(senders=senders)

    async def _propios(_tid):
        return {"+15550003333"}

    monkeypatch.setattr(alta, "_numeros_del_negocio", _propios)
    cuenta, disponibles = await alta.descubrir(uuid.uuid4(), AC, "tok", transport=transport)
    assert cuenta.friendly_name == "Mi cuenta"
    assert [(d.e164, d.seleccionable, d.motivo, d.webhook_ajeno) for d in disponibles] == [
        ("+15550001111", True, "", True),
        ("+15550002222", False, "no_online", False),
        ("+15550003333", True, "ya_conectado_aqui", False),
    ]


@pytest.mark.parametrize(
    ("cuenta", "code"),
    [("suspended", "twilio_account_suspended"), ("closed", "twilio_account_suspended")],
)
async def test_cuenta_no_activa(cuenta, code):
    transport, _ = _twilio(cuenta=cuenta)
    with pytest.raises(alta.AltaError) as exc:
        await alta.descubrir(uuid.uuid4(), AC, "tok", transport=transport)
    assert (exc.value.http_status, exc.value.code) == (409, code)


async def test_credenciales_con_formato_invalido_no_salen_a_red():
    with pytest.raises(alta.AltaError) as exc:
        await alta.descubrir(uuid.uuid4(), "ACcorto", "tok")
    assert exc.value.code == "twilio_credentials_invalid"


async def test_conectar_pide_confirmar_si_el_webhook_es_ajeno():
    transport, estado = _twilio(webhook={"callback_url": "https://otra.app/in"})
    with pytest.raises(alta.AltaError) as exc:
        await alta.conectar(
            uuid.uuid4(), account_sid=AC, auth_token="tok", sender_sid=XE, transport=transport
        )
    assert exc.value.code == "webhook_overwrite_not_confirmed"
    assert exc.value.details == {"previous_webhook": {"callback_url": "https://otra.app/in"}}
    # No tocó el sender.
    assert estado["webhook"] == {"callback_url": "https://otra.app/in"}


async def test_conectar_rechaza_un_sender_que_no_esta_online():
    senders = [{"sid": XE, "sender_id": "whatsapp:+15550001111", "status": "PENDING_VERIFICATION"}]
    transport, _ = _twilio(senders=senders)
    with pytest.raises(alta.AltaError) as exc:
        await alta.conectar(
            uuid.uuid4(), account_sid=AC, auth_token="tok", sender_sid=XE, transport=transport
        )
    assert (exc.value.code, exc.value.details) == (
        "sender_not_online",
        {"status": "PENDING_VERIFICATION"},
    )


async def test_registrar_webhooks_confirma_releyendo():
    transport, estado = _twilio()
    client = TwilioClient(AC, "tok", transport=transport)
    status, error, sender_status, _ = await alta._registrar_webhooks(
        client, XE, "https://t/in", "https://t/st"
    )
    assert (status, error, sender_status) == ("connected", None, "ONLINE")
    # Con los connection overrides en el fragmento (no entran en la firma).
    assert estado["webhook"]["callback_url"] == "https://t/in#rc=3&rp=5xx,ct"
    assert estado["webhook"]["status_callback_method"] == "POST"


async def test_registrar_webhooks_con_token_revocado():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"code": 20003, "message": "denied"})
    )
    client = TwilioClient(AC, "tok", transport=transport)
    status, error, *_ = await alta._registrar_webhooks(client, XE, "https://t/in", "https://t/st")
    assert (status, error) == ("revoked", 20003)
