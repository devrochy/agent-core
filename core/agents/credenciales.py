"""Credenciales en las tools declarativas: qué es sensible y dónde hay literales (ADR-0113 §2).

Decisión del dueño (2026-10-11, adenda de HU-AOS-13 en ADR-0113): además de la lista cerrada
del contrato (`SENSITIVE_HEADERS`), toda credencial de una tool declarativa v3 se declara por
referencia y su valor vive cifrado en la instalación `core`:

- **headers** con nombre sensible → `{"secret": "<nombre>"}` (el contrato ya lo admite);
- **URL:** el userinfo (`https://user:pass@host`) no se admite (el lector lo pasa a un header
  `Authorization: Basic` por referencia, que es lo que hace httpx con el userinfo), y un
  parámetro de query con nombre sensible lleva como valor COMPLETO `{secret:<nombre>}`. Es el
  único lugar de la URL donde se admite esa forma: ni en el host, ni en la ruta, ni mezclada;
- **body:** una clave con nombre sensible (o anidada bajo una, salvo claves neutras como
  `user` o `type`) lleva como valor `{"secret": "<nombre>"}`;
- **parameters:** el JSON Schema que ve el LLM no lleva credenciales (`default`, `const`,
  `enum` ni `examples` bajo una propiedad sensible);
- el fragmento de la URL no viaja: no puede llevar credenciales.

Un nombre es sensible si alguna de sus palabras (separadas por guiones, `_`, puntos o
camelCase, sin tildes) es o empieza por `auth`, `token`, `secret`, `passw`, `passphrase`,
`passcode`, `session`, `cookie`, `signature`, `credential`, `bearer`, `clave`, `contrasen`,
`llave`, `firma` o `hmac`; termina en `key`, `keys`, `token`, `secret`, `password`, `passwd`,
`pwd`, `cookie`, `auth`, `session`, `sessionid` o `sessid`; es `key`, `pass`, `pw`, `pwd`,
`sig`, `sid`, `jwt`, `pin` u `otp`; o está en `SENSITIVE_HEADERS`. Ante la duda, sensible.

Un valor de query o de body (que se interpolan con los argumentos) se clasifica:
- **argumento:** solo `{argumento}`, con un esquema opcional delante (`Bearer {token}`);
- **literal:** texto fijo sin placeholders. Se mira el valor CRUDO: `%7B` no es una llave;
- **mixto:** texto fijo y placeholders (`sk_live{x}`). No se puede pasar a un secreto sin
  cambiar lo que se envía: el linter lo rechaza y el lector falla con la ruta.
Los números y booleanos no cuentan (pasarlos a un secreto, texto, cambiaría el tipo del JSON;
`max_tokens: 500` es común): límite documentado. Un header no se interpola: todo texto no
vacío de un header sensible es literal.

Convención del valor: un secreto guarda el valor SIN codificar, sea de un header, de la query o
del body; el runtime v3 lo codifica al ponerlo en la query (`quote(safe="")`) y sustituye
argumentos y secretos en una sola pasada, comprobando el host (HU-AOS-14/15). El loop legacy
no usa este módulo y no cambia.

Puro: lo usan el linter (`contrato_v3.lint_agentspec`: CLI, CI y API), el lector tolerante y el
backfill. Ningún resultado lleva valores: solo rutas.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from collections.abc import Callable, Iterator
from typing import Any, Literal
from urllib.parse import unquote, unquote_plus

from agentcore_contracts import SENSITIVE_HEADERS, SecretRef

_PREFIJOS = (
    "auth", "token", "secret", "passw", "passphrase", "passcode", "session", "cookie", "signature",
    "credential", "bearer", "clave", "contrasen", "llave", "firma", "hmac",
)  # fmt: skip
_SUFIJOS = (
    "key", "keys", "token", "secret", "password", "passwd", "pwd", "cookie", "auth", "session",
    "sessionid", "sessid",
)  # fmt: skip
_EXACTOS = frozenset({"key", "pass", "pw", "pwd", "sig", "sid", "jwt", "pin", "otp"})
# Claves que bajo un objeto sensible (`"auth": {...}`) siguen siendo dato, no credencial.
_NEUTRAS = frozenset(
    {"user", "username", "usuario", "login", "email", "type", "tipo", "scheme", "name", "nombre", "id",
     "account", "cuenta", "scope", "scopes", "grant_type", "method", "metodo", "client_id", "clientid"}
)  # fmt: skip
_SEPARADORES_RE = re.compile(r"[^A-Za-z0-9]+")
_PALABRAS_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_COMPACTOS = frozenset(h.replace("-", "") for h in SENSITIVE_HEADERS)
_SECRET_NAME_RE = re.compile(r"[a-z0-9_]{1,60}")
# `{argumento}` (lo interpola `core/mcp/declarative.py`) y `{secret:nombre}` (referencia en la query).
_ARGUMENTO_RE = re.compile(r"\{[a-zA-Z0-9_]+\}")
SECRET_PLACEHOLDER_RE = re.compile(r"\{secret:([a-z0-9_]{1,60})\}")
_CUALQUIER_REF_RE = re.compile(r"\{secret:", re.IGNORECASE)
_ESQUEMA_RE = re.compile(r"^(bearer|basic|token|bot|apikey|key)\s*", re.IGNORECASE)
_SCHEMA_VALORES = ("default", "const", "enum", "examples", "example")

Clase = Literal["vacio", "argumento", "referencia", "literal", "mixto", "otro"]


def _sin_tildes(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def palabras(nombre: str) -> list[str]:
    limpio = _sin_tildes(nombre)
    return [p.lower() for trozo in _SEPARADORES_RE.split(limpio) for p in _PALABRAS_RE.findall(trozo)]


def es_sensible(nombre: str) -> bool:
    """¿El nombre de un header, parámetro o clave del body es de una credencial?"""
    bajo = _sin_tildes(nombre).lower()
    if bajo in SENSITIVE_HEADERS or _SEPARADORES_RE.sub("", bajo) in _COMPACTOS:
        return True
    return any(
        p in _EXACTOS or p.startswith(_PREFIJOS) or p.endswith(_SUFIJOS) for p in palabras(nombre)
    )


def clasificar(valor: Any) -> Clase:
    """Clase de un valor interpolable (query o body), mirando el texto CRUDO."""
    if not isinstance(valor, str):
        return "otro"
    if not valor.strip():
        return "vacio"
    if SECRET_PLACEHOLDER_RE.fullmatch(valor):
        return "referencia"
    if not (_ARGUMENTO_RE.search(valor) or _CUALQUIER_REF_RE.search(valor)):
        return "literal"
    resto = SECRET_PLACEHOLDER_RE.sub("", _ARGUMENTO_RE.sub("", valor)).strip()
    resto = _ESQUEMA_RE.sub("", resto).strip()
    return "argumento" if not resto and not _CUALQUIER_REF_RE.search(valor) else "mixto"


def es_literal(valor: Any) -> bool:
    """Texto fijo (no vacío, sin placeholders). Compatibilidad: `clasificar(v) == "literal"`."""
    return clasificar(valor) == "literal"


def _es_ref(valor: Any) -> str | None:
    """Nombre de una referencia `{"secret": "<nombre>"}` VÁLIDA (el nombre cumple el patrón):
    un `{"secret": <otra cosa>}` es dato, y su texto nunca se trata (ni se reporta) como nombre."""
    if isinstance(valor, SecretRef):
        return valor.secret
    if (
        isinstance(valor, dict)
        and set(valor) == {"secret"}
        and isinstance(valor["secret"], str)
        and _SECRET_NAME_RE.fullmatch(valor["secret"])
    ):
        return valor["secret"]
    return None


# ------------------------------------------------------------------ URL
def _partes_url(url: str) -> tuple[str, str, str, str, str, str]:
    """`(esquema://, userinfo, autoridad, ruta, query, fragmento)` sin reparsear (así se
    conservan los `{argumentos}` tal cual)."""
    esquema, sep, resto = url.partition("://")
    if not sep:
        return "", "", "", url, "", ""
    fin = len(resto)
    for c in "/?#":
        i = resto.find(c)
        if i != -1:
            fin = min(fin, i)
    autoridad, cola = resto[:fin], resto[fin:]
    userinfo = ""
    if "@" in autoridad:
        userinfo, _, autoridad = autoridad.rpartition("@")
    cola, _, fragmento = cola.partition("#")
    ruta, _, query = cola.partition("?")
    return f"{esquema}://", userinfo, autoridad, ruta, query, fragmento


_SEP_QUERY_RE = re.compile(r"([&;])")


def _trozos(query: str) -> list[str]:
    """Partes y separadores de la query (`&` y `;`: hay servidores que parten por los dos)."""
    return _SEP_QUERY_RE.split(query) if query else []


def _problemas_query(query: str, prefijo: str) -> Iterator[tuple[str, str]]:
    for parte in _trozos(query):
        if parte in ("&", ";") or not parte:
            continue
        crudo, igual, valor = parte.partition("=")
        nombre = unquote_plus(crudo)
        clase = clasificar(valor)
        if clase == "referencia" and not es_sensible(nombre):
            continue  # una referencia también vale en un parámetro de nombre neutro
        if _CUALQUIER_REF_RE.search(crudo) or (clase == "mixto" and _CUALQUIER_REF_RE.search(valor)):
            yield f"{prefijo}.{nombre}", "referencia_mal_ubicada"
        elif es_sensible(nombre) and igual and clase in ("literal", "mixto"):
            yield f"{prefijo}.{nombre}", "credencial_literal" if clase == "literal" else "credencial_mixta"


def problemas_de_url(url: str) -> list[tuple[str, str]]:
    """`(ruta, código)` de las credenciales o referencias mal puestas en la URL. Sin valores."""
    _, userinfo, autoridad, ruta, query, fragmento = _partes_url(url)
    problemas: list[tuple[str, str]] = []
    if userinfo:
        problemas.append(("userinfo", "credencial_literal"))
    if _CUALQUIER_REF_RE.search(autoridad) or _CUALQUIER_REF_RE.search(ruta):
        problemas.append(("ruta", "referencia_mal_ubicada"))
    problemas += list(_problemas_query(query, "query"))
    if fragmento:
        problemas += [(r, "credencial_literal") for r, c in _problemas_query(fragmento, "fragmento")
                      if c != "referencia_mal_ubicada"]  # fmt: skip
        if _CUALQUIER_REF_RE.search(fragmento):
            problemas.append(("fragmento", "referencia_mal_ubicada"))
    return problemas


def credenciales_en_url(url: str) -> list[str]:
    """Rutas (sin valores) de credenciales o referencias mal puestas en la URL."""
    return [r for r, _ in problemas_de_url(url)]


def referencias_en_url(url: str) -> set[str]:
    """Referencias válidas: solo como valor completo de un parámetro de query."""
    _, _, _, _, query, _ = _partes_url(url)
    refs: set[str] = set()
    for parte in _trozos(query):
        _, _, valor = parte.partition("=")
        if (m := SECRET_PLACEHOLDER_RE.fullmatch(valor)) is not None:
            refs.add(m.group(1))
    return refs


# ------------------------------------------------------------------ body
def _hijo_sensible(clave: str, padre: bool) -> bool:
    return es_sensible(clave) or (padre and clave.lower() not in _NEUTRAS)


def _recorrer(valor: Any, ruta: str, sensible: bool, con_refs: bool) -> Iterator[tuple[str, Any, bool]]:
    yield ruta, valor, sensible
    if isinstance(valor, dict) and not (con_refs and _es_ref(valor) is not None):
        for k, v in valor.items():
            yield from _recorrer(v, f"{ruta}.{k}" if ruta else str(k), _hijo_sensible(str(k), sensible), con_refs)
    elif isinstance(valor, list):
        for i, v in enumerate(valor):
            yield from _recorrer(v, f"{ruta}[{i}]", sensible, con_refs)


def problemas_de_body(body: Any, *, con_refs: bool = True) -> list[tuple[str, str]]:
    """`(ruta, código)` de credenciales literales o mixtas y de `{secret:` en textos del body.
    `con_refs = False` (body v2): `{"secret": ...}` es dato, no referencia."""
    problemas: list[tuple[str, str]] = []
    for ruta, valor, sensible in _recorrer(body, "", False, con_refs):
        if isinstance(valor, str) and _CUALQUIER_REF_RE.search(valor):
            problemas.append((ruta, "referencia_mal_ubicada"))
        elif sensible:
            clase = clasificar(valor)
            if clase in ("literal", "mixto"):
                problemas.append((ruta, "credencial_literal" if clase == "literal" else "credencial_mixta"))
    return problemas


def credenciales_en_body(body: Any, *, con_refs: bool = True) -> list[str]:
    return [r for r, _ in problemas_de_body(body, con_refs=con_refs)]


def referencias_en_body(body: Any) -> set[str]:
    return {n for _, v, _ in _recorrer(body, "", False, True) if (n := _es_ref(v)) is not None}


# ------------------------------------------------------------------ parameters (JSON Schema)
def problemas_de_parameters(schema: Any, ruta: str = "parameters") -> list[tuple[str, str]]:
    """Valores fijos (`default`, `const`, `enum`, `examples`) bajo propiedades sensibles: el
    schema viaja al proveedor del LLM en cada turno."""
    problemas: list[tuple[str, str]] = []
    props = schema.get("properties") if isinstance(schema, dict) else None
    for nombre, prop in props.items() if isinstance(props, dict) else ():
        if not isinstance(prop, dict):
            continue
        aqui = f"{ruta}.properties.{nombre}"
        if es_sensible(str(nombre)):
            for clave in _SCHEMA_VALORES:
                valores = prop.get(clave)
                valores = valores if isinstance(valores, list) else [valores]
                if any(clasificar(v) in ("literal", "mixto") for v in valores):
                    problemas.append((f"{aqui}.{clave}", "credencial_literal"))
        problemas += problemas_de_parameters(prop, aqui)
        if isinstance(prop.get("items"), dict):
            problemas += problemas_de_parameters(prop["items"], f"{aqui}.items")
    return problemas


# ------------------------------------------------------------------ tool completa
def referencias_de_tool(tool: Any, *, v2: bool = False) -> set[str]:
    """Secretos que usa una tool declarativa (`DeclarativeTool` o su dict): headers, valor de
    query y body. En un dict v2 (`v2=True`) solo los headers: v2 no tiene referencias en la URL
    ni en el body (`{"secret": ...}` ahí es dato)."""
    if isinstance(tool, dict):
        headers, url, body = tool.get("headers") or {}, str(tool.get("url") or ""), tool.get("body") or {}
    else:
        headers, url, body = tool.headers, tool.url, tool.body
    refs = {n for v in headers.values() if (n := _es_ref(v)) is not None}
    if v2:
        return refs
    return refs | referencias_en_url(url) | referencias_en_body(body)


def problemas_de_tool(tool: dict[str, Any]) -> list[tuple[str, str]]:
    """`(ruta relativa a la tool, código)` de toda credencial literal o mixta y de toda
    referencia mal puesta (`headers.X`, `url.query.k`, `body.a.b`, `parameters...`). Sin valores."""
    problemas: list[tuple[str, str]] = []
    for h, v in (tool.get("headers") or {}).items():
        if isinstance(v, str) and _CUALQUIER_REF_RE.search(v):
            problemas.append((f"headers.{h}", "referencia_mal_ubicada"))
        elif es_sensible(h) and isinstance(v, str) and v.strip():
            problemas.append((f"headers.{h}", "credencial_literal"))
    problemas += [(f"url.{r}", c) for r, c in problemas_de_url(str(tool.get("url") or ""))]
    problemas += [(f"body.{r}", c) for r, c in problemas_de_body(tool.get("body") or {})]
    problemas += problemas_de_parameters(tool.get("parameters") or {})
    return problemas


def credenciales_literales(tool: dict[str, Any]) -> list[str]:
    return [r for r, _ in problemas_de_tool(tool)]


# ------------------------------------------------------------------ extracción (lector v2)
class CredencialMixta(ValueError):
    """Una credencial mezcla texto fijo con argumentos (o está donde no se puede migrar sin
    cambiar lo que se envía). El mensaje lleva solo rutas."""


def extraer_de_url(url: str, nuevo_nombre: Callable[[str], str]) -> tuple[str, dict[str, str], str | None]:
    """Saca las credenciales literales de una URL v2. Devuelve `(url, {secreto: valor sin
    codificar} de la query, valor de `Authorization: Basic` del userinfo o None)`. El
    fragmento se quita (no viaja). `CredencialMixta` si una credencial mezcla texto y
    argumentos, o si el userinfo lleva argumentos."""
    inicio, userinfo, autoridad, ruta, query, _fragmento = _partes_url(url)
    mixtas: list[str] = []
    basic = None
    if userinfo:
        if _ARGUMENTO_RE.search(userinfo):
            mixtas.append("url.userinfo")
        else:
            usuario, _, clave = userinfo.partition(":")
            crudo = f"{unquote(usuario)}:{unquote(clave)}".encode()
            basic = "Basic " + base64.b64encode(crudo).decode()
    secretos: dict[str, str] = {}
    partes: list[str] = []
    for parte in _trozos(query):
        crudo, igual, valor = parte.partition("=")
        nombre = unquote_plus(crudo)
        clase = clasificar(valor)
        if parte in ("&", ";") or not igual or not es_sensible(nombre) or clase not in ("literal", "mixto"):
            partes.append(parte)
        elif clase == "mixto":
            mixtas.append(f"url.query.{nombre}")
            partes.append(parte)
        else:
            ref = nuevo_nombre(nombre)
            secretos[ref] = unquote_plus(valor)
            partes.append(f"{crudo}={{secret:{ref}}}")
    if mixtas:
        raise CredencialMixta(", ".join(mixtas))
    nueva = inicio + autoridad + ruta + (("?" + "".join(partes)) if query else "")
    return nueva, secretos, basic


def extraer_de_body(body: Any, nuevo_nombre: Callable[[str], str]) -> tuple[Any, dict[str, str]]:
    """El body v2 con cada texto fijo bajo una clave sensible cambiado por `{"secret": ...}`.
    En v2 `{"secret": ...}` es dato: también se extrae. `CredencialMixta` si un valor sensible
    mezcla texto y argumentos."""
    secretos: dict[str, str] = {}
    mixtas: list[str] = []

    def _copia(valor: Any, ruta: str, sensible: bool) -> Any:
        if sensible:
            clase = clasificar(valor)
            if clase == "literal":
                ref = nuevo_nombre(ruta)
                secretos[ref] = valor
                return {"secret": ref}
            if clase == "mixto":
                mixtas.append(f"body.{ruta}")
                return valor
        if isinstance(valor, dict):
            return {
                k: _copia(v, f"{ruta}_{k}" if ruta else str(k), _hijo_sensible(str(k), sensible))
                for k, v in valor.items()
            }
        if isinstance(valor, list):
            return [_copia(v, f"{ruta}_{i}", sensible) for i, v in enumerate(valor)]
        return valor

    salida = _copia(body, "", False)
    if mixtas:
        raise CredencialMixta(", ".join(mixtas))
    return salida, secretos


__all__ = [
    "SECRET_PLACEHOLDER_RE",
    "CredencialMixta",
    "clasificar",
    "credenciales_en_body",
    "credenciales_en_url",
    "credenciales_literales",
    "es_literal",
    "es_sensible",
    "extraer_de_body",
    "extraer_de_url",
    "palabras",
    "problemas_de_body",
    "problemas_de_parameters",
    "problemas_de_tool",
    "problemas_de_url",
    "referencias_de_tool",
    "referencias_en_body",
    "referencias_en_url",
]
