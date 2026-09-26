"""OAuth de Google Calendar: scopes necesarios (ADR-0070/0071)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from pack_calendar.oauth import SCOPES, build_authorization_url


def test_scopes_incluyen_email_para_userinfo():
    # Sin userinfo.email, `oauth2/v2/userinfo` devuelve 401 y no hay external_id.
    assert "https://www.googleapis.com/auth/userinfo.email" in SCOPES
    assert "https://www.googleapis.com/auth/calendar.events" in SCOPES


def test_authorization_url_pide_ambos_scopes_y_offline():
    params = parse_qs(urlparse(build_authorization_url("cid")).query)
    assert params["scope"][0] == SCOPES
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
