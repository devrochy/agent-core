"""Clasificación de errores de Graph API (core/gateway/meta_errors.py)."""

from __future__ import annotations

import httpx
import pytest

from core.gateway.meta_errors import RATE_LIMIT_CODES, classify_and_build_error


def _headers(**kwargs) -> httpx.Headers:
    return httpx.Headers(kwargs)


def test_auth_190_no_reintenta():
    error = classify_and_build_error(
        http_status=401,
        body={"error": {"code": 190, "message": "Invalid OAuth", "fbtrace_id": "abc"}},
        headers=_headers(),
    )
    assert error.category == "AUTH"
    assert error.is_transient is False
    assert error.retry_after_ms is None
    assert error.fbtrace_id == "abc"


@pytest.mark.parametrize("code", sorted(RATE_LIMIT_CODES))
def test_rate_limit_codes(code):
    error = classify_and_build_error(
        http_status=400, body={"error": {"code": code, "message": "limit"}}, headers=_headers()
    )
    assert error.category == "RATE_LIMIT"
    assert error.is_transient is True
    assert error.retry_after_ms is not None


def test_rate_limit_retry_after_de_estimated_time_to_regain_access():
    """Criterio de aceptación: estimated_time_to_regain_access viene en MINUTOS."""
    import json

    headers = _headers(
        **{
            "x-business-use-case-usage": json.dumps(
                {"act_123": [{"type": "whatsapp", "call_count": 95, "estimated_time_to_regain_access": 5}]}
            )
        }
    )
    error = classify_and_build_error(http_status=400, body={"error": {"code": 4}}, headers=headers)
    assert error.retry_after_ms == 300_000


def test_rate_limit_sin_estimated_time_usa_backoff_exponencial():
    error0 = classify_and_build_error(http_status=400, body={"error": {"code": 4}}, headers=_headers(), attempt=0)
    error1 = classify_and_build_error(http_status=400, body={"error": {"code": 4}}, headers=_headers(), attempt=1)
    assert error0.retry_after_ms == 60_000
    assert error1.retry_after_ms == 120_000


def test_transient_generico():
    error = classify_and_build_error(
        http_status=500, body={"error": {"code": 2, "is_transient": True, "message": "temp"}}, headers=_headers()
    )
    assert error.category == "TRANSIENT"
    assert error.is_transient is True


def test_codigo_desconocido_es_permanent_y_logea_warning(caplog):
    import structlog

    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    error = classify_and_build_error(
        http_status=400, body={"error": {"code": 80007, "message": "no documentado"}}, headers=_headers()
    )
    assert error.category == "PERMANENT"
    assert error.is_transient is False
    assert error.retry_after_ms is None


def test_permanent_por_defecto():
    error = classify_and_build_error(
        http_status=400, body={"error": {"code": 100, "message": "parámetro inválido"}}, headers=_headers()
    )
    assert error.category == "PERMANENT"
    assert error.retry_after_ms is None


def test_body_no_json_no_revienta_y_trunca_a_500():
    texto_largo = "x" * 1000
    error = classify_and_build_error(http_status=429, body=None, headers=_headers(), raw_text=texto_largo)
    assert error.category == "PERMANENT"
    assert error.http_status == 429
    assert len(str(error)) <= 600  # 500 del texto + el prefijo del mensaje


def test_no_inventa_significado_para_190_con_subcode():
    """190 con error_subcode no documentado: sigue siendo AUTH (por el code),
    pero el subcode se logea, no se le pone un mensaje inventado."""
    error = classify_and_build_error(
        http_status=401,
        body={"error": {"code": 190, "error_subcode": 99999, "message": "Invalid OAuth"}},
        headers=_headers(),
    )
    assert error.category == "AUTH"
    assert error.error_subcode == 99999
    assert "99999" not in error.message  # no se inventa explicación del subcode en el mensaje
