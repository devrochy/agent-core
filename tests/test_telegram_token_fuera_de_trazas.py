"""El token del bot de Telegram va en la ruta de la Bot API (`/bot<token>/método`): ni las trazas de OTel ni Sentry lo
reciben (ADR-0199, ADR-0188 §4)."""

from __future__ import annotations

import json

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from core.observability import tracing
from core.observability.sentry_setup import _before_send, _event_scrubber

TOKEN = "123456789:AAH" + "x" * 32


@pytest.mark.parametrize(
    ("url", "esperada"),
    [
        (f"https://api.telegram.org/bot{TOKEN}/getMe", "https://api.telegram.org/bot[redactado]/getMe"),
        (
            f"https://api.telegram.org/file/bot{TOKEN}/voice/file_1.oga",
            "https://api.telegram.org/file/bot[redactado]/voice/file_1.oga",
        ),
        # Otro host con una ruta `/bot...` no se toca.
        ("https://ejemplo.com/bot123/abc", "https://ejemplo.com/bot123/abc"),
    ],
)
def test_sin_token_de_telegram(url, esperada):
    assert tracing.sin_token_de_telegram(url) == esperada


def test_el_span_de_httpx_no_lleva_el_token():
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))
    cliente = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True})))
    HTTPXClientInstrumentor.instrument_client(cliente, tracer_provider=proveedor, request_hook=tracing._hook_httpx)
    try:
        cliente.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": 1, "text": "hola"})
    finally:
        HTTPXClientInstrumentor.uninstrument_client(cliente)

    (span,) = exportador.get_finished_spans()
    texto = json.dumps({"name": span.name, "attributes": dict(span.attributes or {})})
    assert TOKEN not in texto
    assert TOKEN.split(":")[1] not in texto


def test_sentry_tapa_el_token_en_breadcrumbs_y_mensajes():
    event = {
        "breadcrumbs": {"values": [{"data": {"url": f"https://api.telegram.org/bot{TOKEN}/getMe"}}]},
        "exception": {"values": [{"value": f"fallo con {TOKEN}"}]},
    }
    limpio = _before_send(event, {})
    assert limpio is not None and TOKEN not in json.dumps(limpio)


def test_sentry_tapa_las_claves_del_token_y_el_secreto():
    event = {"request": {"data": {"bot_token": TOKEN, "webhook_secret": "s" * 43}}}
    _event_scrubber().scrub_event(event)
    texto = json.dumps(event, default=str)
    assert TOKEN not in texto and "s" * 43 not in texto
