"""Anti-SSRF de `ScopedHttp` y de las tools declarativas del tenant (ADR-0052).

Un admin del tenant controla `url` y `allowed_domains` de sus tools declarativas, y
la respuesta vuelve al LLM y al chat. `ScopedHttp` rechaza hosts internos sin DNS
(una etiqueta, numéricos no canónicos, sufijos internos), resuelve el nombre y
rechaza si ALGUNA IP no es pública, y conecta a la IP ya validada con `Host`/SNI
del nombre original (sin segunda resolución: evita DNS rebinding).

La resolución DNS se simula siempre (`_resolve_host`); el transporte, con
`httpx.MockTransport`.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx
import pytest

import core.mcp.types as mcp_types
from core.mcp.declarative import rejected_domains, validate_declarative_tools
from core.mcp.types import ScopedHttp, is_public_host, plugin_http, public_fqdn


@pytest.fixture
def net(monkeypatch):  # type: ignore[no-untyped-def]
    """DNS simulado (`dns[host] = [ips]`) y transporte que registra lo enviado."""
    state: dict[str, Any] = {
        "dns": {},
        "resolved": [],
        "sent": [],
        "clients": [],
        "fail_ips": {},
        "tried": [],
    }

    async def _resolve(host: str, port: int, timeout: float) -> list[Any]:
        state["resolved"].append((host, port))
        if host not in state["dns"]:
            raise OSError("Name or service not known")
        return [ipaddress.ip_address(ip) for ip in state["dns"][host]]

    def handler(req: httpx.Request) -> httpx.Response:
        state["tried"].append(req.url.host)
        if req.url.host in state["fail_ips"]:
            raise state["fail_ips"][req.url.host]("sin ruta")
        state["sent"].append(req)
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient

    def factory(**kw: Any) -> httpx.AsyncClient:
        state["clients"].append(kw)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(mcp_types, "_resolve_host", _resolve)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


# ------------------------------------------------------------ chequeo sin DNS


@pytest.mark.parametrize(
    "host",
    [
        # Docker / red interna: una sola etiqueta.
        "redis",
        "postgres",
        "api",
        # Formas numéricas que glibc resuelve como IPv4.
        "2130706433",
        "127.1",
        "0x7f.1",
        "0x7f000001",
        "0177.0.0.1",
        "1.2.3.4.5",
        # Sufijos internos y metadata de cloud.
        "localhost",
        "app.localhost",
        "metadata.google.internal",
        "printer.local",
        "router.home.arpa",
        # IPs literales no públicas.
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "100.64.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",
        "64:ff9b::a9fe:a9fe",
        "2002:a9fe:a9fe::1",
        # Etiquetas mal formadas.
        "-evil.com",
        "a..com",
        # TLD que no es de verdad (red de Docker/Coolify).
        "db.solvershub_default",
        "redis.x1",
    ],
)
def test_hosts_internos_o_raros_no_son_publicos(host: str) -> None:
    assert not is_public_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "www.googleapis.com",
        "api.negocio.com",
        "API.Negocio.com.",
        "8.8.8.8",
        "2001:4860:4860::8888",
    ],
)
def test_hosts_publicos(host: str) -> None:
    assert is_public_host(host)


def test_public_fqdn_normaliza_y_rechaza_ips() -> None:
    assert public_fqdn(" API.Negocio.com. ") == "api.negocio.com"
    assert public_fqdn("8.8.8.8") is None  # FQDN: ninguna IP literal
    assert public_fqdn("redis") is None
    assert public_fqdn("münch.de") is None  # IDN: se escribe en punycode
    assert public_fqdn("xn--mnch-0ra.de") == "xn--mnch-0ra.de"


# ------------------------------------------------------------ request con DNS


@pytest.mark.parametrize(
    "url",
    [
        "http://redis:6379/",
        "http://2130706433/",
        "http://127.1/",
        "http://0x7f.1/",
        "http://[::ffff:169.254.169.254]/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
async def test_request_rechaza_host_interno_sin_resolver(net, url: str) -> None:
    host = httpx.URL(url).host
    # Aunque el tenant lo ponga en su allowlist.
    r = await ScopedHttp([host, "internal", "1"]).request("GET", url)
    assert "Dominio no autorizado" in r["error"]
    assert net["resolved"] == []
    assert net["sent"] == []


@pytest.mark.parametrize(
    "ips",
    [
        ["169.254.169.254"],  # metadata de cloud
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["172.18.0.4"],  # red de Docker
        ["100.64.0.7"],  # CGNAT
        ["::1"],
        ["::ffff:10.0.0.1"],
        ["93.184.215.14", "10.0.0.5"],  # basta una IP privada
    ],
)
async def test_request_rechaza_dns_que_resuelve_a_ip_no_publica(net, ips: list[str]) -> None:
    net["dns"]["169.254.169.254.nip.io"] = ips
    r = await ScopedHttp(["nip.io"]).request(
        "GET", "http://169.254.169.254.nip.io/latest/meta-data"
    )
    assert "Dominio no autorizado" in r["error"]
    assert net["resolved"] == [("169.254.169.254.nip.io", 80)]
    assert net["sent"] == []
    assert "169.254" not in r["error"].replace("169.254.169.254.nip.io", "")  # no filtra la IP


async def test_request_dns_que_no_resuelve(net) -> None:
    r = await ScopedHttp(["api.negocio.com"]).request("GET", "https://api.negocio.com/x")
    # Mismo error que una IP interna (no deja enumerar nombres internos).
    assert r == {"error": "Dominio no autorizado: https://api.negocio.com/x"}
    assert net["sent"] == []


async def test_request_conecta_a_la_ip_validada_con_host_y_sni(net) -> None:
    net["dns"]["api.negocio.com"] = ["93.184.215.14"]
    r = await ScopedHttp(["negocio.com"]).request(
        "GET",
        "https://API.negocio.com/orders/1",
        params={"q": "a"},
        headers={"Authorization": "Bearer t", "host": "169.254.169.254"},
    )
    assert r == {"status": 200, "data": {"ok": True}}
    assert net["resolved"] == [("api.negocio.com", 443)]
    [req] = net["sent"]
    # Se conecta a la IP resuelta (sin segunda resolución que pueda cambiar)...
    assert req.url.host == "93.184.215.14"
    assert req.url.path == "/orders/1" and req.url.params["q"] == "a"
    # ...pero el Host y el SNI son del dominio (el certificado se valida contra él);
    # el `Host` del tenant no pisa al real.
    assert req.headers["host"] == "api.negocio.com"
    assert req.extensions["sni_hostname"] == "api.negocio.com"
    assert req.headers["authorization"] == "Bearer t"
    # Sin redirects (podrían llevar a una IP interna) ni proxies del entorno.
    assert net["clients"][-1]["follow_redirects"] is False
    assert net["clients"][-1]["trust_env"] is False


async def test_request_puerto_explicito_y_http(net) -> None:
    net["dns"]["api.negocio.com"] = ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"]
    r = await ScopedHttp(["api.negocio.com"]).request(
        "POST", "http://api.negocio.com:8080/x", json_body={"a": 1}
    )
    assert r["status"] == 200
    assert net["resolved"] == [("api.negocio.com", 8080)]
    [req] = net["sent"]
    assert req.url.host == "2606:2800:21f:cb07:6820:80da:af6b:8b2c" and req.url.port == 8080
    assert req.headers["host"] == "api.negocio.com:8080"
    assert "sni_hostname" not in req.extensions


async def test_request_prueba_la_siguiente_ip_si_no_conecta(net) -> None:
    v6, v4 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c", "93.184.215.14"
    net["dns"]["api.negocio.com"] = [v6, v4]
    # IPv4 primero; si su connect vence, se prueba la IPv6.
    net["fail_ips"][v4] = httpx.ConnectTimeout
    r = await ScopedHttp(["api.negocio.com"]).request("GET", "https://api.negocio.com/")
    assert r["status"] == 200
    assert net["tried"] == [v4, v6]
    assert net["sent"][0].extensions["timeout"]["connect"] == 5.0

    net["fail_ips"][v6] = httpx.ConnectError
    r = await ScopedHttp(["api.negocio.com"]).request("GET", "https://api.negocio.com/")
    assert r == {"error": "Fallo de red: ConnectError"}


async def test_request_valida_con_el_mismo_parser_que_conecta(net) -> None:
    """`evil.com\\@x.com`: urllib y httpx no coinciden en el host; se usa el de httpx
    para validar y para conectar, así no hay diferencia que aprovechar."""
    net["dns"]["x.com"] = ["93.184.215.14"]
    r = await ScopedHttp(["x.com"]).request("GET", "https://evil.com\\@x.com/")
    assert r["status"] == 200
    assert net["sent"][0].headers["host"] == "x.com"


async def test_plugin_http_de_pack_calendar_sigue_funcionando(net) -> None:
    net["dns"]["www.googleapis.com"] = ["142.250.78.10"]
    net["dns"]["oauth2.googleapis.com"] = ["142.250.78.10"]
    http = plugin_http(["www.googleapis.com", "oauth2.googleapis.com"])
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        r = await http.request(
            method, "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        )
        assert r["status"] == 200, method
    r = await http.request("POST", "https://oauth2.googleapis.com/token", data={"grant_type": "x"})
    assert r["status"] == 200
    assert {req.headers["host"] for req in net["sent"]} == {
        "www.googleapis.com",
        "oauth2.googleapis.com",
    }


# ------------------------------------------------------- tools declarativas


def test_validate_declarative_tools_solo_deja_fqdn_publicos() -> None:
    raw = [
        {
            "name": "consulta",
            "url": "https://api.negocio.com/x",
            "allowed_domains": [
                "API.negocio.com",
                "api.negocio.com.",
                "redis",
                "127.1",
                "2130706433",
                "169.254.169.254",
                "metadata.google.internal",
                "localhost",
            ],
        },
        {"name": "texto", "url": "https://a.com", "allowed_domains": "a.com"},  # no lista
    ]
    clean = validate_declarative_tools(raw)
    assert clean[0]["allowed_domains"] == ["api.negocio.com"]
    assert clean[1]["allowed_domains"] == []
    assert rejected_domains(raw) == [
        "redis",
        "127.1",
        "2130706433",
        "169.254.169.254",
        "metadata.google.internal",
        "localhost",
    ]


async def test_tool_declarativa_hacia_hostname_de_docker_no_sale(net) -> None:
    """El caso del hallazgo: el tenant apunta su tool a `redis` / `api` internos."""
    import uuid

    from core.mcp.declarative import build_tool
    from core.mcp.types import ToolContext

    [definition] = validate_declarative_tools(
        [{"name": "fuga", "url": "http://api:8000/admin/{x}", "allowed_domains": ["api"]}]
    )
    assert definition["allowed_domains"] == []
    tool = build_tool(definition)
    ctx = ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key="k",
        channel="whatsapp",
    )
    r = await tool.handler({"x": "1"}, ctx)
    assert "Dominio no autorizado" in r["error"]
    assert net["resolved"] == [] and net["sent"] == []
