"""Parseo de uso de Graph API y allowlist de logging (core/gateway/meta_usage.py)."""

from __future__ import annotations

import json

import httpx

from core.gateway.meta_usage import parse_usage, safe_log_headers


def _headers(**kwargs) -> httpx.Headers:
    return httpx.Headers(kwargs)


def test_parse_usage_x_app_usage():
    headers = _headers(**{"x-app-usage": json.dumps({"call_count": 42, "total_cputime": 10, "total_time": 5})})
    snapshot = parse_usage(headers)
    assert snapshot.app_usage == {"call_count": 42, "total_cputime": 10, "total_time": 5}
    assert snapshot.worst_pct == 42


def test_parse_usage_worst_pct_es_el_maximo_real():
    headers = _headers(
        **{
            "x-business-use-case-usage": json.dumps(
                {
                    "act_1": [
                        {"type": "whatsapp", "call_count": 30, "total_cputime": 55, "total_time": 12},
                        {"type": "whatsapp", "call_count": 91, "total_cputime": 20, "total_time": 8},
                    ]
                }
            )
        }
    )
    snapshot = parse_usage(headers)
    assert snapshot.worst_pct == 91


def test_parse_usage_sin_headers_no_rompe():
    snapshot = parse_usage(_headers())
    assert snapshot.worst_pct == 0
    assert snapshot.app_usage == {}
    assert snapshot.business_usage == {}


def test_parse_usage_header_invalido_no_rompe():
    snapshot = parse_usage(_headers(**{"x-app-usage": "no es json"}))
    assert snapshot.worst_pct == 0


def test_safe_log_headers_descarta_authorization_y_cookie():
    headers = _headers(
        **{
            "Authorization": "Bearer secreto",
            "Cookie": "session=abc",
            "X-App-Usage": '{"call_count": 10}',
            "X-Business-Use-Case-Usage": "{}",
            "X-FB-Trace-Id": "trace123",
            "Content-Type": "application/json",
        }
    )
    safe = safe_log_headers(headers)
    assert set(safe.keys()) == {"x-app-usage", "x-business-use-case-usage", "x-fb-trace-id"}
    assert "authorization" not in safe
    assert "cookie" not in safe
    assert "content-type" not in safe
