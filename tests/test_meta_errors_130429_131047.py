"""130429 (throughput excedido) clasifica como RATE_LIMIT con backoff;
131047 (re-engagement) clasifica PERMANENT, nunca reintenta."""

from __future__ import annotations

import httpx

from core.gateway.meta_errors import REENGAGEMENT_WINDOW_CODE, classify_and_build_error


def _headers(usage_json: str | None = None) -> httpx.Headers:
    if usage_json:
        return httpx.Headers({"x-business-use-case-usage": usage_json})
    return httpx.Headers({})


def test_130429_clasifica_rate_limit_con_backoff():
    body = {"error": {"code": 130429, "message": "Rate limit hit"}}
    exc = classify_and_build_error(http_status=400, body=body, headers=_headers())
    assert exc.category == "RATE_LIMIT"
    assert exc.is_transient is True
    assert exc.retry_after_ms is not None


def test_130429_usa_estimated_time_to_regain_access():
    body = {"error": {"code": 130429, "message": "Rate limit hit"}}
    usage = '{"business_use_case": [{"type": "whatsapp", "estimated_time_to_regain_access": 3}]}'
    exc = classify_and_build_error(http_status=400, body=body, headers=_headers(usage))
    assert exc.retry_after_ms == 3 * 60_000


def test_131047_clasifica_permanent_sin_reintento():
    body = {"error": {"code": REENGAGEMENT_WINDOW_CODE, "message": "Re-engagement message"}}
    exc = classify_and_build_error(http_status=400, body=body, headers=_headers())
    assert exc.category == "PERMANENT"
    assert exc.is_transient is False
    assert exc.retry_after_ms is None
    assert exc.code == REENGAGEMENT_WINDOW_CODE
    assert "plantilla" in exc.message.lower()
