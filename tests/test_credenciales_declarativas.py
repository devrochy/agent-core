"""Credenciales de las tools declarativas (decisión del dueño 2026-10-11, adenda de HU-AOS-13
en ADR-0113): qué nombre es sensible, dónde hay literales y el linter v3 que los rechaza."""

from __future__ import annotations

from typing import Any

import pytest

from core.agents.contrato_v3 import revisar_spec
from core.agents.credenciales import (
    clasificar,
    credenciales_en_body,
    credenciales_en_url,
    credenciales_literales,
    es_literal,
    es_sensible,
    referencias_de_tool,
)
from core.kernel.procedures import catalogo_de_acciones


@pytest.mark.parametrize(
    "nombre",
    [
        # La lista del contrato, sin distinguir mayúsculas.
        "Authorization", "COOKIE", "x-api-key", "Proxy-Authorization", "Private-Token",
        # El patrón amplio.
        "X-Goog-Api-Key", "apikey", "api_key", "accessToken", "access_token", "X-Shopify-Access-Token",
        "client_secret", "X-Client-Secret", "password", "passwd", "pwd", "X-Session", "sessionid",
        "X-Hub-Signature-256", "sig", "X-Credentials", "bearer", "jwt", "csrfToken", "key", "pass",
        # En español y variantes comunes.
        "clave", "clave_api", "contraseña", "contrasena", "llave", "firma", "X-Firma", "passphrase", "passcode",
        "hmac", "pin", "otp", "api_keys", "apiKeys", "jsessionid", "PHPSESSID", "sid", "connect.sid",
        # Ambiguos: mejor sensibles.
        "Idempotency-Key", "Author", "token_type",
    ],
)
def test_nombres_sensibles(nombre: str) -> None:
    assert es_sensible(nombre)


@pytest.mark.parametrize(
    "nombre",
    [
        "Content-Type", "Accept", "Accept-Language", "X-Request-Id", "X-Correlation-Id", "User-Agent",
        "Cache-Control", "If-None-Match", "X-Cliente", "keyword", "page", "q", "formato", "guia", "monto",
    ],
)
def test_falsos_positivos_razonables(nombre: str) -> None:
    assert not es_sensible(nombre)


def test_literal_es_texto_fijo() -> None:
    assert es_literal("abc")
    assert not es_literal("")
    assert not es_literal("   ")
    assert not es_literal("{guia}")  # viene de los argumentos
    assert not es_literal("Bearer {token}")
    assert not es_literal("{secret:mi_token}")
    assert not es_literal(1234)  # un número no pasa a secreto sin cambiar el tipo del JSON
    assert not es_literal(True)
    # Placeholders sobre el valor CRUDO: el texto fijo pegado a un argumento no es "argumento".
    assert clasificar("sk_live_x{_}") == "mixto"
    assert clasificar("SECRETO%7Bx%7D") == "literal"
    assert clasificar("Bearer {token}") == "argumento"
    assert clasificar("{secret:k}") == "referencia"
    assert clasificar("x{secret:k}") == "mixto"


def test_credenciales_en_url() -> None:
    assert credenciales_en_url("https://api.x.co/v1/{id}") == []
    assert credenciales_en_url("https://u:p@api.x.co/v1") == ["userinfo"]
    assert credenciales_en_url("https://token@api.x.co/v1") == ["userinfo"]
    assert credenciales_en_url("https://api.x.co/v1?api_key=abc&q=zapatos&token={t}&sig={secret:s}") == [
        "query.api_key"
    ]
    assert credenciales_en_url("https://api.x.co/v1?keyword=zapatos&page=2") == []
    assert credenciales_en_url("https://api.x.co/v1?a=1;token=T") == ["query.token"]
    assert credenciales_en_url("https://api.x.co/v1#access_token=T") == ["fragmento.access_token"]
    assert credenciales_en_url("https://api.x.co/v1?api_key=sk{x}") == ["query.api_key"]
    # `{secret:..}` solo como valor completo de la query.
    assert credenciales_en_url("https://api.x.co/{secret:k}/v1") == ["ruta"]
    assert credenciales_en_url("https://{secret:k}.x.co/v1") == ["ruta"]


def test_credenciales_en_body() -> None:
    body: dict[str, Any] = {
        "q": "zapatos",
        "auth": {"user": "u", "password": "p"},
        "api_key": "k",
        "token": "{t}",
        "pin": 1234,
        "tokens": ["a", "{b}"],
        "ya": {"secret": {"secret": "ref"}},
    }
    assert credenciales_en_body(body) == ["auth.password", "api_key", "tokens[0]"]
    # La sensibilidad se hereda por objetos, salvo claves neutras (user, type...).
    assert credenciales_en_body({"auth": {"type": "basic", "data": "X"}, "credentials": [{"value": "Y"}]}) == [
        "auth.data",
        "credentials[0].value",
    ]
    # `{"secret": <no-nombre>}` es dato: su texto nunca se toma como nombre de secreto.
    assert credenciales_en_body({"client": {"secret": "Abc123!"}}) == ["client.secret"]
    assert credenciales_en_body({"x": "Bearer {secret:k}"}) == ["x"]


def test_referencias_de_tool_en_headers_url_y_body() -> None:
    tool = {
        "url": "https://api.x.co/v1?api_key={secret:a}",
        "headers": {"Authorization": {"secret": "b"}, "X-Cliente": "c"},
        "body": {"auth": {"password": {"secret": "c"}}},
    }
    assert referencias_de_tool(tool) == {"a", "b", "c"}
    assert referencias_de_tool(tool, v2=True) == {"b"}
    assert referencias_de_tool({"url": "https://x.co/{secret:k}?a=b{secret:j}", "body": {"s": {"secret": "Ab!"}}}) == set()
    assert credenciales_literales(tool) == []


def _spec(tool: dict[str, Any]) -> dict[str, Any]:
    base = {
        "name": "consultar_envio",
        "url": "https://api.x.co/v1",
        "allowedDomains": ["api.x.co"],
        "headers": {},
        "body": {},
    }
    return {"contract": "agentspec/3", "model": {"primary": "m"}, "declarativeTools": [base | tool]}


@pytest.fixture(scope="module")
def catalogo():  # type: ignore[no-untyped-def]
    return catalogo_de_acciones([])


@pytest.mark.parametrize(
    ("tool", "ruta"),
    [
        ({"headers": {"X-Goog-Api-Key": "literal"}}, "declarativeTools[0].headers.X-Goog-Api-Key"),
        ({"url": "https://u:p@api.x.co/v1"}, "declarativeTools[0].url.userinfo"),
        ({"url": "https://api.x.co/v1?access_token=abc"}, "declarativeTools[0].url.query.access_token"),
        ({"body": {"credentials": {"secret_key": "abc"}}}, "declarativeTools[0].body.credentials.secret_key"),
        ({"url": "https://api.x.co/v1#token=abc"}, "declarativeTools[0].url.fragmento.token"),
        (
            {"parameters": {"type": "object", "properties": {"api_key": {"type": "string", "default": "abc"}}}},
            "declarativeTools[0].parameters.properties.api_key.default",
        ),
    ],
)
def test_el_linter_rechaza_credenciales_literales(catalogo, tool: dict[str, Any], ruta: str) -> None:  # type: ignore[no-untyped-def]
    issues = revisar_spec(_spec(tool), catalogo)
    assert [(i.code, i.path) for i in issues] == [("credencial_literal", ruta)]
    # El mensaje nunca repite el valor.
    assert all("abc" not in i.message and "u:p" not in i.message for i in issues)


def test_el_linter_acepta_referencias_y_lo_que_no_es_credencial(catalogo) -> None:  # type: ignore[no-untyped-def]
    tool = {
        "url": "https://api.x.co/v1/{guia}?api_key={secret:api}&formato=json",
        "headers": {"X-Goog-Api-Key": {"secret": "goog"}, "Content-Type": "application/json", "X-Request-Id": "r"},
        "body": {"guia": "{guia}", "auth": {"password": {"secret": "pw"}}, "keyword": "zapatos"},
    }
    assert revisar_spec(_spec(tool), catalogo) == []


def test_secretos_en_url_o_body_solo_por_https(catalogo) -> None:  # type: ignore[no-untyped-def]
    issues = revisar_spec(_spec({"url": "http://api.x.co/v1?api_key={secret:api}"}), catalogo)
    assert [(i.code, i.path) for i in issues] == [("secreto_sin_https", "declarativeTools[0].url")]


@pytest.mark.parametrize(
    ("tool", "codigo", "ruta"),
    [
        ({"url": "https://api.x.co/v1?api_key=abc{x}"}, "credencial_mixta", "declarativeTools[0].url.query.api_key"),
        ({"body": {"password": "abc{x}"}}, "credencial_mixta", "declarativeTools[0].body.password"),
        ({"url": "https://api.x.co/{secret:k}"}, "referencia_mal_ubicada", "declarativeTools[0].url.ruta"),
        ({"headers": {"X-Cliente": "{secret:k}"}}, "referencia_mal_ubicada", "declarativeTools[0].headers.X-Cliente"),
        ({"body": {"q": "Bearer {secret:k}"}}, "referencia_mal_ubicada", "declarativeTools[0].body.q"),
    ],
)
def test_el_linter_rechaza_mezclas_y_referencias_mal_puestas(catalogo, tool, codigo, ruta) -> None:  # type: ignore[no-untyped-def]
    assert [(i.code, i.path) for i in revisar_spec(_spec(tool), catalogo)] == [(codigo, ruta)]


def test_el_linter_rechaza_tools_v2_en_extensions(catalogo) -> None:  # type: ignore[no-untyped-def]
    spec = _spec({}) | {"extensions": {"tools": [{"name": "x", "headers": {"X-Api-Key": "k"}}]}}
    assert [(i.code, i.path) for i in revisar_spec(spec, catalogo)] == [("extension_reservada", "extensions.tools")]
