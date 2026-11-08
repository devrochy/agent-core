"""ADR-0186 (PR-1 de WhatsApp vía Twilio): `ProviderError` clasifica por categoría y
`decide_retry` decide por ella, no por el código de cada proveedor. Meta sigue igual: sus
regresiones viven en `test_outbound_send_task.py` y `test_meta_errors*.py`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.gateway.channels.base import (
    InboundAdapter,
    InboundMessage,
    OutboundAdapter,
    ProviderError,
    category_for_http_status,
)
from core.gateway.meta_errors import GraphError
from core.shared.exceptions import ChannelError
from core.tasks.outbound_send import CATEGORIAS_SIN_REINTENTO, MAX_ATTEMPTS, decide_retry


def _error(category, *, retry_after=None, code=None) -> ProviderError:
    return ProviderError(
        "falló", provider="twilio", code=code, category=category, retry_after=retry_after
    )


@pytest.mark.parametrize(
    ("http_status", "esperada"),
    [
        (400, "PERMANENT"),
        (404, "PERMANENT"),
        (429, "RATE_LIMIT"),
        (500, "TRANSIENT"),
        (503, "TRANSIENT"),
        (None, "TRANSIENT"),
    ],
)
def test_sin_categoria_sale_del_estado_http(http_status, esperada):
    assert category_for_http_status(http_status) == esperada
    assert ProviderError("x", http_status=http_status).category == esperada


def test_un_4xx_desconocido_no_reintenta_y_un_5xx_si():
    assert (
        decide_retry(ProviderError("x", http_status=422), attempts_made=0).action
        == "fail_permanent"
    )
    assert decide_retry(ProviderError("x", http_status=502), attempts_made=0).action == "retry"
    assert decide_retry(ProviderError("x"), attempts_made=0).action == "retry"


def test_provider_error_es_channel_error_y_graph_error_es_provider_error():
    exc = GraphError(
        "rate", category="RATE_LIMIT", code=130429, http_status=400, retry_after_ms=180_000
    )
    assert isinstance(exc, ProviderError)
    assert isinstance(exc, ChannelError)
    assert exc.provider == "meta"
    assert exc.category == "RATE_LIMIT"  # la categoría explícita gana al estado HTTP
    assert exc.retry_after == 180.0
    assert exc.retry_after_ms == 180_000
    assert exc.message == "rate"


@pytest.mark.parametrize("category", sorted(CATEGORIAS_SIN_REINTENTO))
def test_categorias_sin_reintento_fallan_al_primer_intento(category):
    assert decide_retry(_error(category), attempts_made=0).action == "fail_permanent"


def test_rate_limit_respeta_retry_after_y_si_no_viene_espera_60_s():
    assert decide_retry(_error("RATE_LIMIT", retry_after=7), attempts_made=0).countdown_s == 7
    assert decide_retry(_error("RATE_LIMIT"), attempts_made=0).countdown_s == 60.0


def test_quota_reintenta_a_la_hora_salvo_otro_plazo():
    d = decide_retry(_error("QUOTA"), attempts_made=0)
    assert (d.action, d.countdown_s) == ("retry", 3600.0)
    assert decide_retry(_error("QUOTA", retry_after=120), attempts_made=0).countdown_s == 120
    assert decide_retry(_error("QUOTA"), attempts_made=MAX_ATTEMPTS).action == "fail_permanent"


def test_ambiguous_reintenta_una_sola_vez():
    assert decide_retry(_error("AMBIGUOUS"), attempts_made=0).action == "retry"
    assert decide_retry(_error("AMBIGUOUS"), attempts_made=1).action == "fail_permanent"


def test_transient_de_otro_proveedor_usa_el_backoff_de_meta():
    meta = GraphError("t", category="TRANSIENT", is_transient=True)
    for intento in range(MAX_ATTEMPTS):
        assert decide_retry(_error("TRANSIENT"), attempts_made=intento) == decide_retry(
            meta, attempts_made=intento
        )
    assert decide_retry(_error("TRANSIENT"), attempts_made=MAX_ATTEMPTS).action == "fail_permanent"


def test_el_codigo_131047_solo_cuenta_si_es_de_meta():
    """El 131047 de Meta no reintenta ni con otra categoría; un código igual de otro
    proveedor decide por su categoría."""
    assert (
        decide_retry(GraphError("v", category="TRANSIENT", code=131047), attempts_made=0).action
        == "fail_permanent"
    )
    assert decide_retry(_error("TRANSIENT", code=131047), attempts_made=0).action == "retry"


def test_policy_se_difiere_aunque_venga_de_otro_proveedor():
    assert decide_retry(_error("POLICY"), attempts_made=MAX_ATTEMPTS).action == "defer"


@pytest.mark.asyncio
async def test_adaptador_sin_plantillas_por_contenido_lanza_channel_error():
    class SoloTexto(OutboundAdapter):
        provider = "x"

        async def send_text(self, credentials, to_external_id, text):
            return "id"

        async def send_image(self, credentials, to_external_id, image_url, caption=""):
            return "id"

    with pytest.raises(ChannelError, match="id de contenido"):
        await SoloTexto().send_content_template({}, "whatsapp:+57300", "HX123", {"1": "Ana"})


def test_los_adaptadores_de_entrada_aceptan_la_url_y_la_ignoran():
    from agentcore_channel_waweb.adapters import WaWebInbound

    from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound
    from core.gateway.webhook_verify import _hmac_hex

    body = b'{"object":"whatsapp_business_account"}'
    firma = {"x-hub-signature-256": "sha256=" + _hmac_hex("secreto", body)}
    assert WhatsAppMetaInbound().verify_request(body, firma, "secreto", url="https://x/y") is True
    assert WhatsAppMetaInbound().verify_request(body, firma, "secreto") is True
    assert WaWebInbound().verify_request(body, {}, "", url="https://x/y") is True

    class Minimo(InboundAdapter):
        def verify_request(self, raw_body, headers, secret, *, url=None):
            return url == "https://x/y"

        def parse_inbound(self, payload) -> list[InboundMessage]:
            return []

    assert Minimo().verify_request(b"", {}, "", url="https://x/y") is True


@pytest.mark.asyncio
async def test_interactivo_con_error_de_meta_conserva_su_categoria(monkeypatch):
    """Antes de ADR-0186 el `GraphError` no era `ChannelError` y salía del `except` del
    interactivo con su categoría. Ahora lo es: un rate limit de Meta al enviar botones tiene
    que seguir reintentándose, no volverse permanente."""
    from test_outbound_send_task import _send_one_fixture

    from core.tasks.outbound_send import _send_one

    async def rate_limit(*a, **k):
        raise GraphError(
            "rate", category="RATE_LIMIT", code=130429, is_transient=True, retry_after_ms=90_000
        )

    adapter = SimpleNamespace(send_interactive=rate_limit)
    msg, tenant_id, calls = _send_one_fixture(monkeypatch, adapter=adapter)
    msg.interactive = {
        "type": "button",
        "body": "¿Confirmas?",
        "buttons": [{"id": "si", "title": "Sí"}],
    }

    error = await _send_one(msg.id, tenant_id)
    assert isinstance(error, GraphError)
    assert error.category == "RATE_LIMIT"
    assert decide_retry(error, attempts_made=0).countdown_s == 90.0
    assert calls["release"] == 1


@pytest.mark.asyncio
async def test_interactivo_invalido_sigue_siendo_permanente(monkeypatch):
    from test_outbound_send_task import _send_one_fixture

    from core.tasks.outbound_send import _send_one

    async def invalido(*a, **k):
        raise ChannelError("Mensaje interactivo inválido")

    adapter = SimpleNamespace(send_interactive=invalido)
    msg, tenant_id, _calls = _send_one_fixture(monkeypatch, adapter=adapter)
    msg.interactive = {"type": "button", "body": "x", "buttons": []}

    error = await _send_one(msg.id, tenant_id)
    assert error.category == "PERMANENT"
    assert "no enviable" in error.message


@pytest.mark.asyncio
async def test_send_one_devuelve_el_error_de_otro_proveedor_tal_cual(monkeypatch):
    from test_outbound_send_task import _send_one_fixture

    from core.tasks.outbound_send import _send_one

    async def ventana(*a, **k):
        raise ProviderError(
            "fuera de ventana", provider="twilio", code=63016, http_status=400, category="WINDOW"
        )

    adapter = SimpleNamespace(send_text=ventana, send_image=ventana)
    msg, tenant_id, _calls = _send_one_fixture(monkeypatch, adapter=adapter)

    error = await _send_one(msg.id, tenant_id)
    assert (error.provider, error.code, error.category) == ("twilio", 63016, "WINDOW")
    assert decide_retry(error, attempts_made=0).action == "fail_permanent"
