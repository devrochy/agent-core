"""Credenciales en las tools declarativas: qué es sensible y dónde hay literales (ADR-0113 §2).

Decisión del dueño (2026-10-11, adenda de HU-AOS-13 en ADR-0113): además de la lista cerrada
del contrato (`SENSITIVE_HEADERS`), toda credencial de una tool declarativa se declara por
referencia y su valor vive cifrado en la instalación `core`:

- **headers** con nombre sensible → `{"secret": "<nombre>"}` (el contrato ya lo admite);
- **URL:** el userinfo (`https://user:pass@host`) no se admite (pasa a un header
  `Authorization: Basic` por referencia, que es lo que hace httpx con el userinfo) y un
  parámetro de query con nombre sensible lleva `{secret:<nombre>}` en el valor;
- **body:** una clave con nombre sensible lleva `{"secret": "<nombre>"}` como valor.

Un nombre es sensible si alguna de sus palabras (separadas por guiones, `_`, puntos o
camelCase) es o empieza por `auth`, `token`, `secret`, `passw`, `session`, `cookie`,
`signature`, `credential` o `bearer`, termina en `key`, `token`, `secret`, `password`,
`passwd`, `pwd`, `cookie`, `auth` o `session`, es `key`, `pass`, `pw`, `pwd`, `sig` o `jwt`,
o si el nombre está en `SENSITIVE_HEADERS`. Ante la duda, sensible: `Idempotency-Key` o
`Author` cuentan; `Content-Type`, `Accept`, `X-Request-Id` y `keyword` no.

Un valor es literal si es un texto no vacío que no viene de los argumentos (sin ningún
`{argumento}`) ni es una referencia. Los números y booleanos no cuentan: no se pueden pasar a
un secreto (texto) sin cambiar el tipo del JSON.

Puro: lo usan el linter (`contrato_v3.lint_agentspec`, CLI, CI y API), el lector tolerante y el
backfill. Resolver las referencias de URL y body al ejecutar es del runtime v3 (HU-AOS-14/15):
el loop legacy sigue con los literales del AgentSpec v2 y no cambia.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import unquote, unquote_plus

from agentcore_contracts import SENSITIVE_HEADERS, SecretRef

_PREFIJOS = ("auth", "token", "secret", "passw", "session", "cookie", "signature", "credential", "bearer")
_SUFIJOS = ("key", "token", "secret", "password", "passwd", "pwd", "cookie", "auth", "session")
_EXACTOS = frozenset({"key", "pass", "pw", "pwd", "sig", "jwt"})
_SEPARADORES_RE = re.compile(r"[^A-Za-z0-9]+")
_PALABRAS_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_COMPACTOS = frozenset(h.replace("-", "") for h in SENSITIVE_HEADERS)
# `{argumento}` (lo interpola `core/mcp/declarative.py`) y `{secret:nombre}` (referencia en la URL).
_ARGUMENTO_RE = re.compile(r"\{[a-zA-Z0-9_]+\}")
SECRET_PLACEHOLDER_RE = re.compile(r"\{secret:([a-z0-9_]{1,60})\}")


def palabras(nombre: str) -> list[str]:
    return [p.lower() for trozo in _SEPARADORES_RE.split(nombre) for p in _PALABRAS_RE.findall(trozo)]


def es_sensible(nombre: str) -> bool:
    """¿El nombre de un header, parámetro o clave del body es de una credencial?"""
    bajo = nombre.lower()
    if bajo in SENSITIVE_HEADERS or _SEPARADORES_RE.sub("", bajo) in _COMPACTOS:
        return True
    for palabra in palabras(nombre):
        if palabra in _EXACTOS or palabra.startswith(_PREFIJOS) or palabra.endswith(_SUFIJOS):
            return True
    return False


def es_literal(valor: Any) -> bool:
    """¿Es un texto fijo (no vacío, sin `{argumento}` ni `{secret:...}`)?"""
    return (
        isinstance(valor, str)
        and bool(valor.strip())
        and not _ARGUMENTO_RE.search(valor)
        and not SECRET_PLACEHOLDER_RE.search(valor)
    )


def _es_ref(valor: Any) -> str | None:
    if isinstance(valor, SecretRef):
        return valor.secret
    if isinstance(valor, dict) and set(valor) == {"secret"} and isinstance(valor["secret"], str):
        return valor["secret"]
    return None


# ------------------------------------------------------------------ URL
def _partes_url(url: str) -> tuple[str, str, str, str, str]:
    """`(esquema://, userinfo, resto de la autoridad + ruta, query, #fragmento)` sin reparsear
    (así se conservan los `{argumentos}` tal cual)."""
    esquema, sep, resto = url.partition("://")
    if not sep:
        return "", "", url, "", ""
    fin = len(resto)
    for c in "/?#":
        i = resto.find(c)
        if i != -1:
            fin = min(fin, i)
    autoridad, cola = resto[:fin], resto[fin:]
    userinfo = ""
    if "@" in autoridad:
        userinfo, _, autoridad = autoridad.rpartition("@")
    cola, hash_, fragmento = cola.partition("#")
    ruta, q, query = cola.partition("?")
    return f"{esquema}://", userinfo, autoridad + ruta, query if q else "", hash_ + fragmento


def _params(query: str) -> list[tuple[str, str, str]]:
    """`(nombre decodificado, nombre crudo, valor crudo)` de cada parámetro (sin `=` → valor '')."""
    salida = []
    for parte in query.split("&") if query else []:
        crudo, _, valor = parte.partition("=")
        salida.append((unquote_plus(crudo), crudo, valor))
    return salida


def credenciales_en_url(url: str) -> list[str]:
    """Rutas (sin valores) de credenciales literales en la URL: `userinfo` o `query.<param>`."""
    _, userinfo, _, query, _ = _partes_url(url)
    rutas = ["userinfo"] if userinfo else []
    rutas += [f"query.{n}" for n, _, v in _params(query) if es_sensible(n) and es_literal(unquote_plus(v))]
    return rutas


def referencias_en_url(url: str) -> set[str]:
    return set(SECRET_PLACEHOLDER_RE.findall(url))


# ------------------------------------------------------------------ body
def _recorrer(valor: Any, ruta: str, sensible: bool) -> Iterator[tuple[str, Any, bool]]:
    yield ruta, valor, sensible
    if isinstance(valor, dict) and _es_ref(valor) is None:
        for k, v in valor.items():
            yield from _recorrer(v, f"{ruta}.{k}" if ruta else str(k), es_sensible(str(k)))
    elif isinstance(valor, list):
        for i, v in enumerate(valor):
            yield from _recorrer(v, f"{ruta}[{i}]", sensible)


def credenciales_en_body(body: Any) -> list[str]:
    """Rutas (sin valores) de textos fijos bajo una clave sensible del body."""
    return [r for r, v, sensible in _recorrer(body, "", False) if sensible and es_literal(v)]


def referencias_en_body(body: Any) -> set[str]:
    return {n for _, v, _ in _recorrer(body, "", False) if (n := _es_ref(v)) is not None}


# ------------------------------------------------------------------ tool completa
def referencias_de_tool(tool: Any) -> set[str]:
    """Secretos que usa una tool declarativa (`DeclarativeTool` o su dict del contrato):
    headers, URL y body."""
    if isinstance(tool, dict):
        headers, url, body = tool.get("headers") or {}, str(tool.get("url") or ""), tool.get("body") or {}
    else:
        headers, url, body = tool.headers, tool.url, tool.body
    refs = {n for v in headers.values() if (n := _es_ref(v)) is not None}
    return refs | referencias_en_url(url) | referencias_en_body(body)


def credenciales_literales(tool: dict[str, Any]) -> list[str]:
    """Rutas relativas a la tool (`headers.X`, `url.userinfo`, `url.query.k`, `body.a.b`) de
    las credenciales con valor literal. Sin valores."""
    rutas = [f"headers.{h}" for h, v in (tool.get("headers") or {}).items() if es_sensible(h) and isinstance(v, str) and v.strip()]
    rutas += [f"url.{r}" for r in credenciales_en_url(str(tool.get("url") or ""))]
    rutas += [f"body.{r}" for r in credenciales_en_body(tool.get("body") or {})]
    return rutas


# ------------------------------------------------------------------ extracción (lector)
def extraer_de_url(url: str, nuevo_nombre: Callable[[str], str]) -> tuple[str, dict[str, str], str | None]:
    """Saca las credenciales literales de la URL. Devuelve `(url sin literales, {secreto:
    valor} de la query, valor de `Authorization: Basic` del userinfo o None)`. El valor de
    query se guarda tal como estaba escrito (codificado): el runtime v3 lo pone igual."""
    inicio, userinfo, medio, query, fragmento = _partes_url(url)
    basic = None
    if userinfo:
        usuario, _, clave = userinfo.partition(":")
        crudo = f"{unquote(usuario)}:{unquote(clave)}".encode()
        basic = "Basic " + base64.b64encode(crudo).decode()
    secretos: dict[str, str] = {}
    partes: list[str] = []
    for parte in query.split("&") if query else []:
        crudo, igual, valor = parte.partition("=")
        nombre = unquote_plus(crudo)
        if igual and es_sensible(nombre) and es_literal(unquote_plus(valor)):
            ref = nuevo_nombre(nombre)
            secretos[ref] = valor
            partes.append(f"{crudo}={{secret:{ref}}}")
        else:
            partes.append(parte)
    nueva = inicio + medio + (("?" + "&".join(partes)) if query else "") + fragmento
    return nueva, secretos, basic


def extraer_de_body(body: Any, nuevo_nombre: Callable[[str], str]) -> tuple[Any, dict[str, str]]:
    """El body con cada texto fijo bajo una clave sensible cambiado por `{"secret": ...}`."""
    secretos: dict[str, str] = {}

    def _copia(valor: Any, ruta: str, sensible: bool) -> Any:
        if sensible and es_literal(valor):
            ref = nuevo_nombre(ruta)
            secretos[ref] = valor
            return {"secret": ref}
        if isinstance(valor, dict) and _es_ref(valor) is None:
            return {k: _copia(v, f"{ruta}_{k}" if ruta else str(k), es_sensible(str(k))) for k, v in valor.items()}
        if isinstance(valor, list):
            return [_copia(v, f"{ruta}_{i}", sensible) for i, v in enumerate(valor)]
        return valor

    return _copia(body, "", False), secretos


__all__ = [
    "SECRET_PLACEHOLDER_RE",
    "credenciales_en_body",
    "credenciales_en_url",
    "credenciales_literales",
    "es_literal",
    "es_sensible",
    "extraer_de_body",
    "extraer_de_url",
    "palabras",
    "referencias_de_tool",
    "referencias_en_body",
    "referencias_en_url",
]
