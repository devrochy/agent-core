"""Firma `X-Twilio-Signature` de los webhooks de Twilio (ADR-0186).

HMAC-SHA1 en base64, con el Auth Token como clave, de la URL completa (con su query) seguida de
cada parámetro POST como `nombre` + `valor`, sin separadores, ordenados por nombre. Es el mismo
algoritmo que `twilio.request_validator.RequestValidator` (twilio-python): los nombres van con
`sorted(set(...))` y los valores repetidos de un mismo nombre, ordenados y sin duplicados exactos.
Como Twilio no siempre firma con el puerto, se acepta la URL con el puerto por defecto y sin él.

La URL es la que se registró en el sender, guardada en la conexión: no se reconstruye desde los
headers del proxy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections import defaultdict
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


def compute_signature(auth_token: str, url: str, params: Iterable[tuple[str, str]]) -> str:
    values: dict[str, set[str]] = defaultdict(set)
    for name, value in params:
        values[name].add(value)
    payload = url + "".join(
        name + value for name in sorted(values) for value in sorted(values[name])
    )
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _url_variants(url: str) -> list[str]:
    parts = urlsplit(url)
    host = parts.hostname or ""
    userinfo, _, _ = parts.netloc.rpartition("@")
    prefix = f"{userinfo}@" if userinfo else ""
    without_port = urlunsplit(parts._replace(netloc=f"{prefix}{host}"))
    if parts.port:
        with_port = url
    else:
        default = 443 if parts.scheme == "https" else 80
        with_port = urlunsplit(parts._replace(netloc=f"{prefix}{host}:{default}"))
    return [without_port, with_port]


def validate_signature(
    auth_token: str, url: str, params: Iterable[tuple[str, str]], signature: str
) -> bool:
    """`True` si la firma corresponde a la URL (con o sin puerto) y a los parámetros. Comparación
    en tiempo constante; sin firma, sin token o sin URL, `False`."""
    if not auth_token or not url or not signature:
        return False
    params = list(params)
    return any(
        hmac.compare_digest(compute_signature(auth_token, variant, params), signature)
        for variant in _url_variants(url)
    )
