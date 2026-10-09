"""Ningún pack sale a red por su cuenta (ADR-0052, docs/PLUGIN-CONTRACT.md §13).

Un pack no importa `httpx` (ni otro cliente HTTP): las tools usan `context.http` y
las rutas sin `ToolContext` (panel, webhooks, jobs), `plugin_http(<allowlist>)`. La
allowlist del módulo (`network_allowlist`) es la verdad en runtime y debe estar
declarada en `network` del manifest (`[tool.agent_core.plugin]`).
"""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[1] / "packages"
PACKS = sorted(p for p in PACKAGES.glob("pack-*") if (p / "pyproject.toml").is_file())
HTTP_CLIENTS = ("httpx", "requests", "aiohttp", "urllib3", "urllib.request", "http.client")


def _is_http_client(name: str) -> bool:
    return any(name == c or name.startswith(f"{c}.") for c in HTTP_CLIENTS)


def test_hay_packs() -> None:
    assert {p.name for p in PACKS} >= {"pack-calendar", "pack-commerce", "pack-payments"}


def test_ningun_pack_importa_un_cliente_http() -> None:
    offenders = []
    for path in sorted(PACKAGES.glob("pack-*/**/*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
                # `from urllib import request`
                names += [f"{node.module}.{a.name}" for a in node.names]
            if any(_is_http_client(n) for n in names):
                offenders.append(f"{path.relative_to(PACKAGES)}:{node.lineno}")
    assert offenders == []


@pytest.mark.parametrize("pack", PACKS, ids=lambda p: p.name)
def test_allowlist_del_modulo_esta_en_el_manifest(pack: Path) -> None:
    manifest = tomllib.loads((pack / "pyproject.toml").read_text())
    network = manifest.get("tool", {}).get("agent_core", {}).get("plugin", {}).get("network", [])
    entry_points = manifest["project"]["entry-points"]["agent_core.plugins"]
    for target in entry_points.values():
        module_name, class_name = target.split(":")
        module = getattr(importlib.import_module(module_name), class_name)
        allowlist = set(module.network_allowlist)
        assert allowlist <= set(network), f"{class_name}: {allowlist - set(network)} no está en `network`"


def _module_allowlist(pack: Path) -> set[str]:
    manifest = tomllib.loads((pack / "pyproject.toml").read_text())
    allowed: set[str] = set()
    for target in manifest["project"]["entry-points"]["agent_core.plugins"].values():
        module_name, class_name = target.split(":")
        allowed |= set(getattr(importlib.import_module(module_name), class_name).network_allowlist)
    return allowed


@pytest.mark.parametrize("pack", PACKS, ids=lambda p: p.name)
def test_plugin_http_del_pack_usa_la_allowlist_del_modulo(pack: Path) -> None:
    """Las rutas sin `ToolContext` no pueden abrir dominios que el módulo no declara:
    cada `plugin_http(X)` del pack recibe una constante igual a su `network_allowlist`."""
    offenders = []
    for path in sorted(pack.glob("*/**/*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "plugin_http":
                continue
            where = f"{path.relative_to(PACKAGES)}:{node.lineno}"
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Name):
                offenders.append(f"{where}: el argumento no es una constante del módulo")
                continue
            module_name = ".".join(path.relative_to(pack).with_suffix("").parts)
            value = getattr(importlib.import_module(module_name), node.args[0].id)
            if set(value) != _module_allowlist(pack):
                offenders.append(f"{where}: {sorted(value)} != network_allowlist")
    assert offenders == []


def test_allowlists_de_los_packs_migrados() -> None:
    from pack_calendar.module import GoogleCalendarModule
    from pack_commerce.module import CRMModule
    from pack_payments.module import PaymentsModule

    assert set(GoogleCalendarModule.network_allowlist) == {"www.googleapis.com", "oauth2.googleapis.com"}
    assert set(CRMModule.network_allowlist) == {"challenges.cloudflare.com"}
    # `checkout.wompi.co` solo lo abre el navegador del pagador.
    assert set(PaymentsModule.network_allowlist) == {"production.wompi.co", "sandbox.wompi.co"}


# ------------------------------------------------------------- pack-commerce


def _mock_transport(monkeypatch, handler):  # type: ignore[no-untyped-def]
    """Hace que el `httpx.AsyncClient` interno de `ScopedHttp` no salga a red."""
    import httpx

    real = httpx.AsyncClient
    created: list[dict] = []

    def factory(**kw):  # type: ignore[no-untyped-def]
        created.append(kw)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return created


async def test_turnstile_sale_por_plugin_http_y_es_fail_closed(monkeypatch) -> None:
    import httpx
    from pack_commerce import api

    seen: list[httpx.Request] = []
    reply: dict = {"response": httpx.Response(200, json={"success": True})}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return reply["response"]

    created = _mock_transport(monkeypatch, handler)

    assert await api._verify_turnstile("tok", "sec", "1.2.3.4") is True
    [req] = seen
    assert str(req.url) == api.TURNSTILE_VERIFY_URL
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert req.content == b"secret=sec&response=tok&remoteip=1.2.3.4"
    assert created[-1]["timeout"] == 8

    # Rechazo de Cloudflare, cuerpo que no es JSON y fallo de red: siempre False.
    reply["response"] = httpx.Response(200, json={"success": False})
    assert await api._verify_turnstile("tok", "sec", None) is False
    reply["response"] = httpx.Response(502, text="bad gateway")
    assert await api._verify_turnstile("tok", "sec", None) is False

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    _mock_transport(monkeypatch, boom)
    assert await api._verify_turnstile("tok", "sec", None) is False
    # Sin token no se llama a la red.
    assert await api._verify_turnstile("", "sec", None) is False


def test_turnstile_fuera_de_la_allowlist_no_sale() -> None:
    from pack_commerce import api

    from core.mcp.types import plugin_http

    http = plugin_http(api.NETWORK)
    assert http._allowed_host(api.TURNSTILE_VERIFY_URL)
    assert not http._allowed_host("https://evil.example.com/siteverify")


# ------------------------------------------------------------- pack-payments


def test_la_tool_de_pagos_pasa_context_http_al_proveedor() -> None:
    from pack_payments.config import PaymentsConfig
    from pack_payments.providers import wompi
    from pack_payments.skills import build_provider

    from core.mcp.types import PLUGIN_HTTP_METHODS

    config = PaymentsConfig.model_validate(
        {"public_key": "pub_test_x", "private_key": "prv_test_x", "events_secret": "e", "sandbox": True}
    )
    sentinel = object()
    assert build_provider(config, http=sentinel).http is sentinel  # type: ignore[arg-type]

    # Sin `context.http` (panel, capacidad de pagos): el cliente del pack.
    http = build_provider(config).http
    assert http._allowed == set(wompi.NETWORK)
    assert http._methods == set(PLUGIN_HTTP_METHODS)
