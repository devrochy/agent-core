"""pack-calendar sale a Google solo por el `ScopedHttp` del core (ADR-0052).

Las tools usan `context.http` (lo arma `plugin_manager.scoped_http`); el panel,
los jobs y el OAuth usan `plugin_http` con la misma allowlist. Ambos se acotan a
la `network_allowlist` del módulo. Los errores siguen siendo `PluginError` con los
mensajes de antes.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from core.mcp.types import PLUGIN_HTTP_METHODS, MCPTool, ScopedHttp, ToolContext, plugin_http
from core.shared.exceptions import PluginError


class _FakeHttp:
    """Cliente simulado: registra las llamadas y devuelve respuestas en orden."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._responses = list(responses)

    async def request(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        self.calls.append((method, url, kw))
        return self._responses.pop(0)


def _context(http: Any) -> ToolContext:
    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key="k",
        channel="whatsapp",
        http=http,
    )


def _mock_transport(monkeypatch, handler) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Hace que el `httpx.AsyncClient` interno de `ScopedHttp` no salga a red."""
    real = httpx.AsyncClient
    created: list[dict[str, Any]] = []

    def factory(**kw: Any) -> httpx.AsyncClient:
        created.append(kw)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return created


# ------------------------------------------------------------------ core


async def test_scoped_http_por_defecto_sigue_en_get_post(monkeypatch) -> None:
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, json={}))
    http = ScopedHttp(["www.googleapis.com"])
    r = await http.request("PATCH", "https://www.googleapis.com/x")
    assert r == {"error": "Método no permitido: PATCH"}


async def test_plugin_http_permite_rest_y_respeta_la_allowlist(monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(204) if req.method == "DELETE" else httpx.Response(200, json={"ok": True})

    created = _mock_transport(monkeypatch, handler)
    http = plugin_http(["www.googleapis.com"])
    for method in PLUGIN_HTTP_METHODS:
        r = await http.request(method, "https://www.googleapis.com/calendar/v3/x")
        assert r["status"] in (200, 204), method
    assert [req.method for req in seen] == list(PLUGIN_HTTP_METHODS)

    r = await http.request("GET", "https://evil.example.com/x")
    assert "Dominio no autorizado" in r["error"]
    assert len(seen) == len(PLUGIN_HTTP_METHODS)  # no salió

    # `timeout` por llamada y cuerpo form-encoded (token endpoint de OAuth2).
    await http.request("POST", "https://www.googleapis.com/t", data={"grant_type": "x"}, timeout=30)
    assert created[-1]["timeout"] == 30
    assert seen[-1].content == b"grant_type=x"
    assert seen[-1].headers["content-type"] == "application/x-www-form-urlencoded"


def test_plugin_manager_scoped_http_usa_la_allowlist_del_modulo() -> None:
    from pack_calendar.module import GoogleCalendarModule

    from core.plugin_manager import PluginManager

    pm = PluginManager()
    pm._modules["pack_calendar"] = GoogleCalendarModule()
    assert pm.scoped_http("pack_calendar")._allowed == {"www.googleapis.com", "oauth2.googleapis.com"}
    # Plugin no detectado (o el core): fail-closed.
    assert pm.scoped_http("otro")._allowed == set()
    assert pm.scoped_http("core")._allowed == set()


def test_loop_arma_el_http_del_pack_y_no_el_de_una_declarativa_homonima(monkeypatch) -> None:
    from core.orchestrator import loop
    from core.plugin_manager import plugin_manager

    async def _noop(arguments, context):  # type: ignore[no-untyped-def]
        return {}

    registered = MCPTool(name="check_availability", description="", parameters={}, handler=_noop)
    registered.plugin_id = "pack_calendar"
    monkeypatch.setattr(loop.mcp_host, "get_tool", lambda name: registered if name == "check_availability" else None)
    sentinel = plugin_http(["www.googleapis.com"])
    asked: list[str] = []
    monkeypatch.setattr(plugin_manager, "scoped_http", lambda pid: asked.append(pid) or sentinel)

    shadow = SimpleNamespace(_allowed_domains=["evil.example.com"])
    assert loop._http_for_tool("check_availability", {"check_availability": shadow}) is sentinel
    assert asked == ["pack_calendar"]

    # Una declarativa pura sigue con su propia allowlist (y solo GET/POST).
    decl = SimpleNamespace(_allowed_domains=["api.negocio.com"])
    http = loop._http_for_tool("consultar_pedido", {"consultar_pedido": decl})
    assert http._allowed == {"api.negocio.com"}
    assert http._methods == {"GET", "POST"}
    # Desconocida: fail-closed.
    assert loop._http_for_tool("inventada", {})._allowed == set()


# ------------------------------------------------------------------ pack


# `test_ningun_pack_importa_un_cliente_http` y la allowlist ⊆ manifest de todos los
# packs viven en tests/test_packs_salida_a_red.py.


def _google_skill(monkeypatch, tokens: list[Any]):  # type: ignore[no-untyped-def]
    from pack_calendar import skills

    cfg = SimpleNamespace(
        timezone="America/Mexico_City",
        calendar_id="agenda@negocio.com",
        business_hours='{"mon": "09:00-18:00", "tue": "09:00-18:00", "wed": "09:00-18:00", '
        '"thu": "09:00-18:00", "fri": "09:00-18:00", "sat": "09:00-18:00", "sun": "09:00-18:00"}',
        slot_minutes=60,
        buffer_minutes=0,
        event_duration_minutes=60,
        client_id="cid",
        client_secret=SecretStr("secret"),
    )
    skill = skills.CalendarSkill()

    async def _source() -> str:
        return "google"

    async def _config() -> Any:
        return cfg

    async def _token(client_id: str, client_secret: str, *, http: Any = None) -> str:
        tokens.append(http)
        return "tok"

    monkeypatch.setattr(skill, "_source", _source)
    monkeypatch.setattr(skill, "_tenant_config", _config)
    monkeypatch.setattr(skills, "get_access_token", _token)
    return skill


async def test_check_availability_consulta_google_por_context_http(monkeypatch) -> None:
    tokens: list[Any] = []
    skill = _google_skill(monkeypatch, tokens)
    day = (date.today() + timedelta(days=7)).isoformat()
    http = _FakeHttp(
        {
            "status": 200,
            "data": {
                "items": [
                    {
                        "start": {"dateTime": f"{day}T10:00:00-06:00"},
                        "end": {"dateTime": f"{day}T11:00:00-06:00"},
                        "summary": "Ocupado",
                    }
                ]
            },
        }
    )

    result = await skill.check_availability({"date": day}, _context(http))

    assert result["status"] == "ok"
    assert [b["summary"] for b in result["busy_slots"]] == ["Ocupado"]
    assert tokens == [http]  # el refresh del token también sale por context.http
    [(method, url, kw)] = http.calls
    assert method == "GET"
    assert url == "https://www.googleapis.com/calendar/v3/calendars/agenda@negocio.com/events"
    assert kw["headers"] == {"Authorization": "Bearer tok"}
    assert kw["timeout"] == 30
    assert kw["params"]["timeMin"] == f"{day}T00:00:00Z"
    assert kw["params"]["singleEvents"] == "true"


async def test_check_availability_error_de_google_es_plugin_error(monkeypatch) -> None:
    skill = _google_skill(monkeypatch, [])
    day = (date.today() + timedelta(days=7)).isoformat()

    http = _FakeHttp({"status": 403, "data": {"error": {"message": "forbidden"}}})
    with pytest.raises(PluginError, match=r"Calendar API 403: .*forbidden"):
        await skill.check_availability({"date": day}, _context(http))

    # Fallo de red / dominio fuera de la allowlist: también PluginError (antes la
    # excepción de httpx la convertía el host en PluginError).
    http = _FakeHttp({"error": "Fallo de red: ReadTimeout"})
    with pytest.raises(PluginError, match="Fallo de red: ReadTimeout"):
        await skill.check_availability({"date": day}, _context(http))


def test_sin_context_http_usa_el_cliente_del_pack() -> None:
    from pack_calendar import google_api
    from pack_calendar.module import GoogleCalendarModule

    from core.plugin_manager import PluginManager

    http = google_api.tool_http(_context(None))
    assert http._allowed == set(GoogleCalendarModule.network_allowlist)
    assert http._methods == set(PLUGIN_HTTP_METHODS)
    # Mismo cliente que el core arma para el `context.http` de las tools.
    pm = PluginManager()
    pm._modules["pack_calendar"] = GoogleCalendarModule()
    core_http = pm.scoped_http("pack_calendar")
    assert (core_http._allowed, core_http._methods) == (http._allowed, http._methods)

    fake = _FakeHttp()
    assert google_api.tool_http(_context(fake)) is fake


async def test_crear_mover_y_borrar_evento() -> None:
    from pack_calendar import google_api

    base = "https://www.googleapis.com/calendar/v3/calendars/cal/events"
    http = _FakeHttp(
        {"status": 200, "data": {"id": "ev1", "htmlLink": "https://calendar.google.com/ev1"}},
        {"status": 200, "data": {"id": "ev1"}},
        {"status": 204, "data": ""},
    )
    event = await google_api.create_event(http, "cal", "tok", {"summary": "Demo"})
    assert event["id"] == "ev1"
    await google_api.patch_event(http, "cal", "tok", "ev1", {"start": {}})
    await google_api.delete_event(http, "cal", "tok", "ev1")

    assert [(m, u) for m, u, _ in http.calls] == [
        ("POST", base),
        ("PATCH", f"{base}/ev1"),
        ("DELETE", f"{base}/ev1"),
    ]
    assert http.calls[0][2]["json_body"] == {"summary": "Demo"}
    assert all(kw["headers"] == {"Authorization": "Bearer tok"} for _, _, kw in http.calls)
    # Los ids van escapados en la ruta.
    assert google_api.event_path("a@b.com", "x/../y?z") == "/calendars/a@b.com/events/x%2F..%2Fy%3Fz"

    # Mismo mensaje que antes (sin el cuerpo) en crear/mover/borrar.
    with pytest.raises(PluginError, match=r"Calendar API 404$"):
        await google_api.delete_event(_FakeHttp({"status": 404, "data": "nf"}), "cal", "tok", "ev1")


async def test_refresh_del_token_sale_por_el_http_recibido(monkeypatch) -> None:
    from pack_calendar import oauth

    saved: list[dict[str, Any]] = []

    async def _tokens() -> dict[str, Any]:
        return {"access_token": "viejo", "refresh_token": "rt", "expires_at": time.time() - 10}

    async def _save(tokens: dict[str, Any], *, from_refresh: bool = False) -> None:
        saved.append(tokens)

    monkeypatch.setattr(oauth, "_connection_tokens", _tokens)
    monkeypatch.setattr(oauth, "save_tokens", _save)

    http = _FakeHttp({"status": 200, "data": {"access_token": "nuevo", "expires_in": 3600}})
    assert await oauth.get_access_token("cid", "secret", http=http) == "nuevo"
    [(method, url, kw)] = http.calls
    assert (method, url) == ("POST", "https://oauth2.googleapis.com/token")
    assert kw["data"]["grant_type"] == "refresh_token"
    assert kw["data"]["refresh_token"] == "rt"
    assert saved[0]["refresh_token"] == "rt"

    http = _FakeHttp({"status": 400, "data": {"error": "invalid_grant"}})
    with pytest.raises(PluginError, match=r"Refresh de token falló: .*invalid_grant"):
        await oauth.get_access_token("cid", "secret", http=http)
