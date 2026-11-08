"""Autoinstrumentación de trazas (B2, ADR-0188): sin `/healthz*`, sin query en las URLs de los spans
(ni del servidor ni de httpx) y sin valores en `db.statement`. Con proveedores de prueba: no se toca el
`TracerProvider` global."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from core.config import get_settings
from core.observability import tracing

SECRETO = "EAAsecretoDeMeta1234567890"


@pytest.fixture
def exportador():
    exp = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exp))
    exp.proveedor = proveedor  # type: ignore[attr-defined]
    return exp


def _todo(spans) -> str:
    return repr([(s.name, dict(s.attributes or {})) for s in spans])


@pytest.mark.parametrize(
    ("url", "esperada"),
    [
        (f"https://graph.facebook.com/v21.0/oauth/access_token?client_secret={SECRETO}", "https://graph.facebook.com/v21.0/oauth/access_token"),
        ("http://user:clave@api.local:8443/x?y=1#frag", "http://api.local:8443/x"),
        ("https://api.openai.com/v1/chat", "https://api.openai.com/v1/chat"),
    ],
)
def test_sin_query(url, esperada):
    assert tracing.sin_query(url) == esperada


def test_fastapi_sin_healthz_y_sin_query(exportador):
    app = FastAPI()

    @app.get("/webhooks/whatsapp")
    async def _verificar(request: dict | None = None) -> dict:
        return {"ok": True}

    @app.get("/healthz")
    async def _h() -> dict:
        return {"status": "ok"}

    @app.get("/healthz/deep")
    async def _hd() -> dict:
        return {"status": "ok"}

    tracing.instrumentar_app(app, tracer_provider=exportador.proveedor)
    cliente = TestClient(app)
    cliente.get(f"/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token={SECRETO}")
    cliente.get("/healthz")
    cliente.get("/healthz/deep")

    spans = exportador.get_finished_spans()
    assert [s.name for s in spans] == ["GET /webhooks/whatsapp"]  # sin send/receive ni /healthz*
    assert SECRETO not in _todo(spans)
    assert spans[0].attributes.get("http.route") == "/webhooks/whatsapp"


def test_httpx_sin_query_en_el_span(exportador):
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    transporte = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    cliente = httpx.Client(transport=transporte)
    HTTPXClientInstrumentor.instrument_client(
        cliente, tracer_provider=exportador.proveedor, request_hook=tracing._hook_httpx
    )
    try:
        cliente.get(f"https://graph.facebook.com/v21.0/oauth/access_token?fb_exchange_token={SECRETO}")
    finally:
        HTTPXClientInstrumentor.uninstrument_client(cliente)

    spans = exportador.get_finished_spans()
    assert len(spans) == 1
    assert SECRETO not in _todo(spans)
    urls = [v for k, v in spans[0].attributes.items() if k in ("http.url", "url.full")]
    assert urls == ["https://graph.facebook.com/v21.0/oauth/access_token"]


def test_hook_async_es_corrutina():
    import inspect

    assert inspect.iscoroutinefunction(tracing._hook_httpx_async)  # httpx descarta los hooks async que no lo son


def test_sqlalchemy_sin_valores_en_el_statement(exportador):
    pytest.importorskip("sqlite3")
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    tracing.instrumentar_engine(engine, tracer_provider=exportador.proveedor)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT :valor AS v"), {"valor": SECRETO})
    finally:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().uninstrument()

    spans = exportador.get_finished_spans()
    assert spans and SECRETO not in _todo(spans)
    assert any("SELECT ?" in str(s.attributes.get("db.statement", "")) for s in spans)


def test_apagado_no_instrumenta(monkeypatch):
    monkeypatch.setattr(get_settings(), "otel_enabled", False)
    app = FastAPI()
    tracing.instrumentar_app(app)
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
    tracing.instrumentar_engine(object())  # no-op: ni siquiera mira el engine
