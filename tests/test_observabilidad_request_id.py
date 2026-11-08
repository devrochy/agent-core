"""`X-Request-ID` y `trace_id` en los logs fuera del turno (B5, ADR-0188)."""

from __future__ import annotations

import json
import logging

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

from core.observability.request_id import RequestIdMiddleware, request_id_de
from core.shared.logger import _formateador_json_de_stdlib, con_trace_id


@pytest.mark.parametrize(
    ("entrante", "se_respeta"),
    [("abc-123_X.y", True), ("a" * 64, True), ("a" * 65, False), ("con espacio", False), ("x\r\nSet-Cookie: y", False),
     (None, False), ("", False)],
)
def test_request_id_de(entrante, se_respeta):
    rid = request_id_de(entrante)
    assert (rid == entrante) is se_respeta
    assert 1 <= len(rid) <= 64


def _app_con_logs(capturados: list[dict]):
    app = FastAPI()

    @app.get("/x")
    async def _x() -> dict:
        capturados.append(structlog.contextvars.get_contextvars())
        return {"ok": True}

    app.add_middleware(RequestIdMiddleware)
    return app


def test_el_middleware_responde_y_liga_el_id():
    capturados: list[dict] = []
    cliente = TestClient(_app_con_logs(capturados))

    r = cliente.get("/x", headers={"X-Request-ID": "del-cliente-1"})
    assert r.headers["x-request-id"] == "del-cliente-1"
    assert capturados[-1]["request_id"] == "del-cliente-1"

    r = cliente.get("/x", headers={"X-Request-ID": "malo\x7f"})
    nuevo = r.headers["x-request-id"]
    assert nuevo != "malo\x7f" and len(nuevo) == 32
    assert capturados[-1]["request_id"] == nuevo
    assert "request_id" not in structlog.contextvars.get_contextvars()  # no queda pegado


def test_la_app_del_core_devuelve_el_id():
    from core.main import app

    r = TestClient(app).get("/healthz")
    assert len(r.headers["x-request-id"]) == 32


def test_con_trace_id_fuera_del_turno_y_sin_pisar_el_del_turno():
    tracer = TracerProvider().get_tracer("prueba")
    assert con_trace_id(None, "info", {"event": "x"}) == {"event": "x"}  # sin span: nada
    with tracer.start_as_current_span("peticion") as span:
        esperado = format(span.get_span_context().trace_id, "032x")
        assert con_trace_id(None, "info", {"event": "x"})["trace_id"] == esperado
        assert con_trace_id(None, "info", {"event": "x", "trace_id": "del-turno"})["trace_id"] == "del-turno"


def test_los_logs_de_stdlib_llevan_request_id_y_trace_id():
    registro = logging.LogRecord("httpx", logging.WARNING, __file__, 1, "fallo", None, None)
    tracer = TracerProvider().get_tracer("prueba")
    with structlog.contextvars.bound_contextvars(request_id="rid-1"), tracer.start_as_current_span("s") as span:
        linea = json.loads(_formateador_json_de_stdlib().format(registro))
    assert linea["request_id"] == "rid-1"
    assert linea["trace_id"] == format(span.get_span_context().trace_id, "032x")
