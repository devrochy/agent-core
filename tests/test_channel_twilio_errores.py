"""Tabla de códigos de Twilio → categoría de `decide_retry` (ADR-0186). Cada fila sale del
diccionario oficial (https://www.twilio.com/docs/api/errors/<código>, 2026-11-07). La tabla se
repite aquí a propósito: cambiar una categoría tiene que ser una decisión, no un accidente."""

from __future__ import annotations

import httpx
import pytest
from agentcore_channel_twilio.errors import (
    CODE_CATEGORIES,
    category_for_code,
    error_from_response,
    error_from_transport,
)

from core.tasks.outbound_send import decide_retry

TABLA = {
    20003: "AUTH", 30002: "AUTH",
    20429: "RATE_LIMIT", 63018: "RATE_LIMIT", 63038: "QUOTA",
    30001: "TRANSIENT", 30008: "TRANSIENT",
    63007: "CONFIG", 63051: "CONFIG", 63112: "CONFIG", 63020: "CONFIG", 21408: "CONFIG",
    63016: "WINDOW",
    21610: "OPT_OUT",
    21211: "RECIPIENT", 21612: "RECIPIENT", 21614: "RECIPIENT", 63003: "RECIPIENT",
    63024: "RECIPIENT", 63032: "RECIPIENT", 63049: "RECIPIENT", 30003: "RECIPIENT",
    30004: "RECIPIENT", 30005: "RECIPIENT", 30006: "RECIPIENT",
    63005: "CONTENT", 63013: "CONTENT", 63021: "CONTENT", 63027: "CONTENT", 63028: "CONTENT",
    63030: "CONTENT", 30007: "CONTENT",
}  # fmt: skip


def test_la_tabla_es_la_revisada():
    assert CODE_CATEGORIES == TABLA


@pytest.mark.parametrize(("code", "categoria"), sorted(TABLA.items()))
def test_cada_codigo_de_la_tabla(code, categoria):
    assert category_for_code(code, 400) == categoria


@pytest.mark.parametrize(
    ("code", "http_status", "esperada"),
    [(99999, 400, "PERMANENT"), (99999, 500, "TRANSIENT"), (None, 503, "TRANSIENT"),
     (None, 429, "RATE_LIMIT"), (None, 401, "AUTH"), (None, 403, "AUTH"), (None, None, "TRANSIENT")],
)  # fmt: skip
def test_un_codigo_desconocido_sale_del_estado_http(code, http_status, esperada):
    assert category_for_code(code, http_status) == esperada


def _response(
    status: int, body: object = None, headers: dict | None = None, text: str = ""
) -> httpx.Response:
    if body is not None:
        return httpx.Response(status, json=body, headers=headers)
    return httpx.Response(status, text=text, headers=headers)


def test_error_de_la_rest_con_su_cuerpo():
    exc = error_from_response(
        _response(400, {"code": 63016, "message": "Outside messaging window", "status": 400})
    )
    assert (exc.provider, exc.code, exc.http_status, exc.category) == (
        "twilio",
        63016,
        400,
        "WINDOW",
    )
    assert "plantilla" in exc.message
    assert decide_retry(exc, attempts_made=0).action == "fail_permanent"


def test_429_reintenta_y_respeta_retry_after_si_llega():
    sin = error_from_response(_response(429, {"code": 20429, "message": "Too many requests"}))
    assert sin.category == "RATE_LIMIT" and sin.retry_after is None
    assert decide_retry(sin, attempts_made=0).countdown_s == 60.0
    con = error_from_response(
        _response(429, {"code": 20429, "message": "x"}, {"Retry-After": "12"})
    )
    assert decide_retry(con, attempts_made=0).countdown_s == 12.0


def test_respuesta_no_json_y_401_sin_codigo():
    html = error_from_response(_response(502, text="<html>bad gateway</html>"))
    assert (html.code, html.category) == (None, "TRANSIENT")
    auth = error_from_response(_response(401, {"message": "Authenticate", "status": 401}))
    assert auth.category == "AUTH"
    assert decide_retry(auth, attempts_made=0).action == "fail_permanent"


def test_el_mensaje_no_lleva_el_token():
    exc = error_from_response(_response(401, {"message": "Authenticate"}))
    assert "token-secreto" not in exc.message


@pytest.mark.parametrize(
    ("exc", "method", "categoria"),
    [
        (httpx.ConnectError("x"), "POST", "TRANSIENT"),
        (httpx.ConnectTimeout("x"), "POST", "TRANSIENT"),
        (httpx.PoolTimeout("x"), "POST", "TRANSIENT"),
        (httpx.ReadTimeout("x"), "POST", "AMBIGUOUS"),
        (httpx.WriteTimeout("x"), "POST", "AMBIGUOUS"),
        (httpx.RemoteProtocolError("x"), "POST", "AMBIGUOUS"),
        (httpx.ReadTimeout("x"), "GET", "TRANSIENT"),
        (httpx.ReadTimeout("x"), "DELETE", "TRANSIENT"),
    ],
)
def test_fallos_de_red(exc, method, categoria):
    assert error_from_transport(exc, method=method).category == categoria


def test_ambiguous_de_twilio_reintenta_una_sola_vez():
    exc = error_from_transport(httpx.ReadTimeout("x"), method="POST")
    assert decide_retry(exc, attempts_made=0).action == "retry"
    assert decide_retry(exc, attempts_made=1).action == "fail_permanent"
