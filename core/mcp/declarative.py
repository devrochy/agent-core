"""Tools declarativas por tenant (ADR-0052).

Un tenant declara tools HTTP en `AgentSpec.extensions["tools"]` (JSON), sin
escribir Python. El core las convierte a `MCPTool` en runtime y las ejecuta con
`ScopedHttp` (allowlist de dominios; nunca el pool ni `environ`).

Forma de cada entrada:
    {
      "name": "consultar_estado_pedido",
      "description": "...",
      "method": "GET" | "POST",
      "url": "https://api.negocio.com/orders/{order_id}",
      "allowed_domains": ["api.negocio.com"],
      "headers": {"Authorization": {"secret": "token_api"}, "X-Cliente": "abc"},  # opcional
      "parameters": { JSON Schema },                 # opcional
      "body": {"order_id": "{order_id}"}             # opcional (POST)
    }

Los `{placeholders}` se interpolan con los argumentos de la tool (validados y
escapados), nunca con texto del LLM sin pasar por argumentos.

Headers sensibles (ADR-0113 §2, HU-AOS-12): un header puede declararse por referencia,
`{"secret": "<nombre>"}`, y su valor se resuelve AL EJECUTAR desde la configuración cifrada
de la instalación `core` del negocio (`core.agents.secretos`). Si falta el secreto, la tool
devuelve un error sin llamar a nadie. Los valores literales de specs v2 viejos siguen
funcionando igual; los editores ya no dejan guardar literales en `Authorization`, `Cookie`
ni `X-Api-Key` (`core.agents.declarativas`).

`allowed_domains` solo admite FQDN públicos (`public_fqdn`): ni IPs, ni hosts de
una etiqueta (`redis`), ni sufijos internos. Al ejecutar, `ScopedHttp` además
resuelve el DNS y rechaza IPs no públicas (anti-SSRF).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

from core.mcp.types import MCPTool, ScopedHttp, ToolContext, public_fqdn
from core.shared.logger import get_logger

log = get_logger(__name__)

PLUGIN_ID = "declarative"
_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,60}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")
_SECRET_NAME_RE = re.compile(r"^[a-z0-9_]{1,60}$")


def secret_ref(value: Any) -> str | None:
    """Nombre del secreto si `value` es una referencia `{"secret": "<nombre>"}` válida."""
    if isinstance(value, dict) and set(value) == {"secret"} and isinstance(value["secret"], str):
        nombre = value["secret"]
        return nombre if _SECRET_NAME_RE.match(nombre) else None
    return None


def _clean_header(value: Any) -> Any:
    nombre = secret_ref(value)
    return {"secret": nombre} if nombre is not None else str(value)


def _clean_domains(raw: Any) -> list[str]:
    """Los `allowed_domains` que son FQDN públicos, normalizados y sin repetir."""
    domains: list[str] = []
    for d in raw if isinstance(raw, list) else []:
        fqdn = public_fqdn(str(d))
        if fqdn and fqdn not in domains:
            domains.append(fqdn)
    return domains


def rejected_domains(raw: Any) -> list[str]:
    """`allowed_domains` que se descartarían al guardar (para avisar en el admin)."""
    rejected: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        domains = item.get("allowed_domains") if isinstance(item, dict) else None
        for d in domains if isinstance(domains, list) else []:
            if str(d).strip() and public_fqdn(str(d)) is None:
                rejected.append(str(d).strip())
    return rejected


def validate_declarative_tools(raw: Any) -> list[dict[str, Any]]:
    """Normaliza/valida las definiciones; descarta las malformadas (fail-safe)."""
    tools: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return tools
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        method = str(item.get("method") or "GET").upper()
        if not _NAME_RE.match(name) or not url.startswith(("http://", "https://")):
            continue
        if method not in ("GET", "POST"):
            continue
        entry: dict[str, Any] = {
            "name": name,
            "description": str(item.get("description") or name)[:400],
            "method": method,
            "url": url,
            "allowed_domains": _clean_domains(item.get("allowed_domains")),
            "headers": {
                str(k): _clean_header(v) for k, v in (item.get("headers") or {}).items()
            }
            if isinstance(item.get("headers"), dict)
            else {},
            "parameters": item.get("parameters")
            if isinstance(item.get("parameters"), dict)
            else {"type": "object", "properties": {}},
            "body": item.get("body") if isinstance(item.get("body"), dict) else {},
        }
        tools.append(entry)
    return tools


def _interpolate(template: str, arguments: dict[str, Any]) -> str:
    """Sustituye `{arg}` con el valor escapado (URL-safe). Sin arg → vacío."""

    def repl(match: re.Match[str]) -> str:
        value = arguments.get(match.group(1), "")
        return quote(str(value), safe="")

    return _PLACEHOLDER_RE.sub(repl, template)


def build_tool(definition: dict[str, Any]) -> MCPTool:
    """Construye un `MCPTool` ejecutable desde una definición declarativa."""
    allowed = definition["allowed_domains"]

    async def _handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        url = _interpolate(definition["url"], arguments)
        crudos = definition.get("headers") or {}
        partes = urlsplit(url)
        if partes.scheme != "https" and any(secret_ref(v) is not None for v in crudos.values()):
            # Un secreto nunca viaja en claro, aunque la tool se haya guardado por otra vía.
            log.warning("declarative_secret_sin_https", tool=definition["name"])
            return {"error": f"La herramienta '{definition['name']}' no tiene configuradas sus credenciales."}
        headers, faltan = await _resolve_headers(crudos, partes.hostname or "")
        if faltan:
            # Sin el secreto (o hacia un dominio al que no puede viajar) no se llama: es un
            # error de configuración del negocio, no del plugin.
            log.warning("declarative_secret_missing", tool=definition["name"], secretos=faltan)
            return {"error": f"La herramienta '{definition['name']}' no tiene configuradas sus credenciales."}
        http = context.http or ScopedHttp(allowed)
        body = {k: _raw_or_interpolated(v, arguments) for k, v in (definition.get("body") or {}).items()}
        result = await http.request(
            definition["method"],
            url,
            json_body=body or None,
            headers=headers or None,
        )
        return result

    tool = MCPTool(
        name=definition["name"],
        description=definition["description"],
        parameters=definition["parameters"],
        handler=_handler,
        plugin_id=PLUGIN_ID,
        scopes=[],
    )
    # La allowlist viaja con la tool para que el loop arme el ScopedHttp del turno.
    tool._allowed_domains = list(allowed)  # type: ignore[attr-defined]
    return tool


async def _resolve_headers(headers: dict[str, Any], host: str) -> tuple[dict[str, str], list[str]]:
    """Headers con las referencias resueltas desde la instalación `core` del negocio en
    contexto, y los nombres de los secretos que faltan (o que no pueden viajar a `host`).
    Sin referencias no toca la base."""
    refs = {k: n for k, v in headers.items() if (n := secret_ref(v)) is not None}
    literales = {k: str(v) for k, v in headers.items() if k not in refs}
    if not refs:
        return literales, []
    from core.agents.secretos import resolver_secretos
    from core.shared.tenant import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    valores = await resolver_secretos(tenant_id, refs.values(), host) if tenant_id is not None else {}
    faltan = sorted({n for n in refs.values() if n not in valores})
    return literales | {k: valores[n] for k, n in refs.items() if n in valores}, faltan


def _raw_or_interpolated(value: Any, arguments: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _interpolate(value, arguments)
    if isinstance(value, dict):
        return {k: _raw_or_interpolated(v, arguments) for k, v in value.items()}
    if isinstance(value, list):
        return [_raw_or_interpolated(v, arguments) for v in value]
    return value


def tools_for_spec(spec) -> list[MCPTool]:  # type: ignore[no-untyped-def]
    """Tools declarativas del tenant (`spec.extensions["tools"]`)."""
    raw = (spec.extensions or {}).get("tools") if spec.extensions else None
    return [build_tool(d) for d in validate_declarative_tools(raw)]
