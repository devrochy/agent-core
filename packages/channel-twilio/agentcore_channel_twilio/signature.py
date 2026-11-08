"""Firma `X-Twilio-Signature` de los webhooks de Twilio.

Twilio firma con el Auth Token de la cuenta: HMAC-SHA1 sobre la URL completa a la que llamó
más, para un POST form-urlencoded, cada parámetro (ordenados por nombre) pegado como
`nombre + valor`, y lo envía en base64. Si el cuerpo es JSON, firma solo la URL, que trae
`bodySHA256` con el SHA-256 del cuerpo.

La URL es la que el core registró en el sender y guardó (ADR-0186), nunca una reconstruida
de los headers: detrás del proxy de Coolify el esquema y el puerto cambian. Mismo criterio
que el `RequestValidator` de twilio-python: se prueba la URL con y sin el puerto por defecto.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, parse_qsl, urlsplit, urlunsplit

from core.gateway.webhook_verify import constant_time_equal


def compute_signature(url: str, params: list[tuple[str, str]], auth_token: str) -> str:
    """Firma esperada para `url` y los parámetros del formulario (con claves repetidas)."""
    data = url
    by_name: dict[str, list[str]] = {}
    for name, value in params:
        by_name.setdefault(name, []).append(value)
    for name in sorted(by_name):
        for value in sorted(by_name[name]):
            data += name + value
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _url_variants(url: str) -> list[str]:
    """La URL tal cual y su variante con o sin el puerto por defecto del esquema."""
    parts = urlsplit(url)
    default_port = {"https": 443, "http": 80}.get(parts.scheme)
    host = parts.hostname or ""
    variants = [url]
    if default_port is None or not host:
        return variants
    if parts.port is None:
        other = parts._replace(netloc=_netloc(parts, f"{host}:{default_port}"))
    elif parts.port == default_port:
        other = parts._replace(netloc=_netloc(parts, host))
    else:
        return variants
    variants.append(urlunsplit(other))
    return variants


def _netloc(parts, host_port: str) -> str:
    userinfo = parts.netloc.rpartition("@")[0]
    return f"{userinfo}@{host_port}" if userinfo else host_port


def _body_hash_matches(url: str, raw_body: bytes) -> bool:
    expected = (parse_qs(urlsplit(url).query).get("bodySHA256") or [""])[0]
    if not expected:
        return False
    return constant_time_equal(hashlib.sha256(raw_body).hexdigest(), expected)


def is_valid(
    url: str, raw_body: bytes, signature: str, auth_token: str, *, content_type: str = ""
) -> bool:
    """¿`signature` es la de Twilio para esta llamada? Sin URL, firma o token: no."""
    if not url or not signature or not auth_token:
        return False
    is_json = "application/json" in content_type.lower()
    if is_json:
        if not _body_hash_matches(url, raw_body):
            return False
        params: list[tuple[str, str]] = []
    else:
        params = parse_qsl(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return any(
        constant_time_equal(compute_signature(variant, params, auth_token), signature)
        for variant in _url_variants(url)
    )
