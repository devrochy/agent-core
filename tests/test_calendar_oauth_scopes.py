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


# --- HU-CAL-07 / ADR-0093: el retorno de Google llega por el panel ------------


class _Settings:
    def __init__(self, public: str, panel: str = "") -> None:
        self.public_base_url = public
        self.panel_base_url = panel


def test_redirect_del_panel_usa_panel_base_url(monkeypatch):
    from pack_calendar import oauth

    monkeypatch.setattr(oauth, "get_settings", lambda: _Settings("https://api.ejemplo", "https://panel.ejemplo/"))
    assert oauth.panel_redirect_uri() == "https://panel.ejemplo/api/calendar/oauth/callback"


def test_redirect_del_panel_sin_panel_base_url_usa_public(monkeypatch):
    from pack_calendar import oauth

    monkeypatch.setattr(oauth, "get_settings", lambda: _Settings("https://test.solvershub.site"))
    assert oauth.panel_redirect_uri() == "https://test.solvershub.site/api/calendar/oauth/callback"


def test_authorization_url_lleva_state_y_redirect_del_panel():
    params = parse_qs(
        urlparse(
            build_authorization_url("cid", redirect="https://p/api/calendar/oauth/callback", state="abc123")
        ).query
    )
    assert params["state"] == ["abc123"]
    assert params["redirect_uri"] == ["https://p/api/calendar/oauth/callback"]
