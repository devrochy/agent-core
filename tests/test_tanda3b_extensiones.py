"""Tanda 3B — extensibilidad de capacidades (ADR-0052).

Canales por entry_points (registry) y tools declarativas por tenant (HTTP acotado).
"""

from __future__ import annotations

import pytest

# --- Canales: el registry built-in + scan de entry_points ------------------


def test_registry_canales_builtin_y_scan() -> None:
    from core.gateway.channels.registry import inbound_adapter_for, outbound_adapter_for, providers

    assert "meta" in providers()
    assert inbound_adapter_for("meta").provider == "meta"
    assert outbound_adapter_for("meta").provider == "meta"
    with pytest.raises(KeyError):
        inbound_adapter_for("inexistente")


# --- Tools declarativas ----------------------------------------------------
def test_validate_declarative_tools_descarta_basura() -> None:
    from core.mcp.declarative import validate_declarative_tools

    clean = validate_declarative_tools(
        [
            {"name": "consulta", "method": "GET", "url": "https://api.x.com/o/{id}",
             "allowed_domains": ["api.x.com"]},
            {"name": "sin_url"},  # sin url -> fuera
            {"name": "bad", "url": "ftp://x"},  # protocolo no permitido
            {"name": "con espacio", "url": "https://y.com"},  # nombre inválido
            {"name": "otra", "method": "DELETE", "url": "https://y.com"},  # método no permitido
        ]
    )
    assert [t["name"] for t in clean] == ["consulta"]
    assert clean[0]["allowed_domains"] == ["api.x.com"]


async def test_scoped_http_deniega_dominio_no_autorizado() -> None:
    from core.mcp.types import ScopedHttp

    http = ScopedHttp(["api.autorizada.com"])
    r = await http.request("GET", "https://otro.com/x")
    assert "error" in r and "Dominio no autorizado" in r["error"]

    # Sin allowlist → fail-closed.
    r2 = await ScopedHttp([]).request("GET", "https://cualquiera.com")
    assert "error" in r2


async def test_scoped_http_bloquea_ssrf() -> None:
    """Aunque el tenant ponga la IP en la allowlist, no se permite (SSRF)."""
    from core.mcp.types import ScopedHttp

    # Metadata de cloud, loopback y rangos privados: bloqueados.
    h = ScopedHttp(["169.254.169.254", "localhost", "10.0.0.5"])
    for url in ("http://169.254.169.254/latest", "http://localhost/x", "http://10.0.0.5/x"):
        r = await h.request("GET", url)
        assert "error" in r, url


async def test_declarative_tool_ejecuta_con_http_mockeado(monkeypatch) -> None:
    import core.mcp.declarative as dec

    calls: dict = {}

    class _FakeHttp:
        def __init__(self, allowed, timeout=15.0):
            calls["allowed"] = allowed

        async def request(self, method, url, **kw):
            calls.update(method=method, url=url, kw=kw)
            return {"status": 200, "data": {"ok": True}}

    monkeypatch.setattr(dec, "ScopedHttp", _FakeHttp)
    import uuid

    from core.mcp.types import ToolContext

    tool = dec.build_tool(
        {
            "name": "consultar_pedido",
            "description": "d",
            "method": "GET",
            "url": "https://api.x.com/orders/{order_id}",
            "allowed_domains": ["api.x.com"],
            "headers": {},
            "parameters": {"type": "object", "properties": {}},
            "body": {},
        }
    )
    ctx = ToolContext(
        conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), idempotency_key="k", channel="whatsapp"
    )
    result = await tool.handler({"order_id": "AB 12"}, ctx)
    assert result["status"] == 200
    # El argumento va escapado en la URL (no inyección).
    assert calls["url"] == "https://api.x.com/orders/AB%2012"


async def test_tools_for_spec_desde_extensions() -> None:
    from agentcore_contracts import AgentSpec

    from core.agents.spec import DEFAULT_SPEC
    from core.mcp.declarative import tools_for_spec

    spec_data = dict(DEFAULT_SPEC)
    spec_data["tenantId"] = "t1"
    spec_data["extensions"] = {
        "tools": [
            {"name": "x", "method": "POST", "url": "https://a.com/y", "allowed_domains": ["a.com"]}
        ]
    }
    spec = AgentSpec.model_validate(spec_data)
    tools = tools_for_spec(spec)
    assert len(tools) == 1 and tools[0].name == "x"
