"""HU-CAL-07 / ADR-0093: conectar Google Calendar desde el panel nuevo.

El retorno de Google llega por una ruta BFF del panel, que llama a la API con la
sesión y el negocio del usuario. La API valida el `state` (anti-CSRF) contra la
cookie `oauth_state` que dejó `/oauth/start`, intercambia el código con la misma
URI de retorno y guarda la conexión SOLO en el negocio del principal.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

START = "/api/v1/pack/calendar/oauth/start"
CALLBACK = "/api/v1/pack/calendar/oauth/callback"


async def _tenant_con_google() -> uuid.UUID:
    from _api_helpers import ADMIN_EMAIL

    from core.onboarding import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    slug = uniq("cal-oauth")
    tenant_id, _ = await onboard_tenant(
        name=f"OAuth {slug}",
        slug=slug,
        business_description="OAuth de prueba.",
        persona={"tone": "cercano"},
        packs=["pack_calendar"],
        admin_email=ADMIN_EMAIL,
    )
    async with tenant_scope(tenant_id):
        await plugin_manager.save_config(
            "pack_calendar",
            {"source": "interno", "client_id": "cid-test", "client_secret": "secret-test"},
        )
    return tenant_id


async def _start(app_client, headers) -> tuple[str, str]:
    r = await app_client.get(START, headers=headers, params={"next": "/calendario"}, follow_redirects=False)
    assert r.status_code == 302, r.text
    state_cookie = r.cookies.get("oauth_state")
    assert state_cookie, "el start debe dejar la cookie oauth_state"
    return r.headers["location"], state_cookie


async def test_start_manda_a_google_con_state_y_retorno_del_panel(app_client) -> None:
    from _api_helpers import api_headers
    from pack_calendar.oauth import panel_redirect_uri

    tenant_id = await _tenant_con_google()
    headers = await api_headers(app_client, tenant_id)
    location, state = await _start(app_client, headers)

    params = parse_qs(urlparse(location).query)
    assert location.startswith("https://accounts.google.com/")
    assert params["redirect_uri"] == [panel_redirect_uri()]
    assert params["redirect_uri"][0].endswith("/api/calendar/oauth/callback")
    assert params["state"] == [state]
    assert params["client_id"] == ["cid-test"]


async def test_callback_sin_autenticacion_no_procesa(app_client) -> None:
    r = await app_client.get(CALLBACK, params={"code": "x", "state": "y"}, follow_redirects=False)
    assert r.status_code == 401


async def test_callback_con_state_invalido_no_intercambia(app_client, monkeypatch) -> None:
    from _api_helpers import api_headers
    from pack_calendar import oauth

    llamadas: list[str] = []

    async def _no_deberia(*args, **kwargs):
        llamadas.append("exchange")
        return {}

    monkeypatch.setattr(oauth, "exchange_code", _no_deberia)
    tenant_id = await _tenant_con_google()
    headers = await api_headers(app_client, tenant_id)
    _location, state = await _start(app_client, headers)

    for cookie in (f"oauth_state={state}x; oauth_next=/calendario", "oauth_next=/calendario"):
        r = await app_client.get(
            CALLBACK,
            headers={**headers, "Cookie": cookie},
            params={"code": "codigo", "state": state},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text
        assert r.headers["location"].endswith("/calendario?google=error")
    assert llamadas == []


async def test_callback_cancelado_en_google(app_client) -> None:
    from _api_helpers import api_headers

    tenant_id = await _tenant_con_google()
    headers = await api_headers(app_client, tenant_id)
    _location, state = await _start(app_client, headers)
    r = await app_client.get(
        CALLBACK,
        headers={**headers, "Cookie": f"oauth_state={state}; oauth_next=/calendario"},
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"].endswith("/calendario?google=cancelado")


async def test_callback_valido_guarda_la_conexion_solo_en_su_negocio(app_client, monkeypatch) -> None:
    from _api_helpers import api_headers
    from pack_calendar import oauth

    from core.gateway.connections import get_active_connection

    capturado: dict[str, str] = {}

    async def _fake_exchange(client_id, client_secret, code, *, redirect=None):
        capturado.update(client_id=client_id, code=code, redirect=redirect or "")
        return {"access_token": "at-x", "refresh_token": "rt-x", "expires_at": 9_999_999_999}

    async def _fake_email(_token: str) -> str:
        return "dueno@ejemplo.com"

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange)
    monkeypatch.setattr(oauth, "_account_email", _fake_email)

    tenant_a = await _tenant_con_google()
    tenant_b = await _tenant_con_google()
    headers = await api_headers(app_client, tenant_a)
    _location, state = await _start(app_client, headers)

    r = await app_client.get(
        CALLBACK,
        headers={**headers, "Cookie": f"oauth_state={state}; oauth_next=/calendario"},
        params={"code": "codigo-google", "state": state},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"].endswith("/calendario?google=ok")
    assert capturado == {
        "client_id": "cid-test",
        "code": "codigo-google",
        "redirect": oauth.panel_redirect_uri(),
    }
    # Las cookies del flujo se borran al terminar.
    borradas = " ".join(r.headers.get_list("set-cookie"))
    assert "oauth_state=" in borradas and "oauth_next=" in borradas

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_a):
        conn_a = await get_active_connection("google_calendar", tenant_a)
    assert conn_a is not None and conn_a.external_id == "dueno@ejemplo.com"
    async with tenant_scope(tenant_b):
        assert await get_active_connection("google_calendar", tenant_b) is None
