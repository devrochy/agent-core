"""Verificación de webhooks de Meta: firma HMAC del body y comparación en
tiempo constante del verify_token del handshake.

`hmac.compare_digest` ya es tiempo-constante en Python -- a diferencia de
`crypto.timingSafeEqual` en Node, no hace falta comprobar longitudes antes
(no lanza con strings de longitud distinta, las trata como no-iguales)."""

from __future__ import annotations

import hashlib
import hmac
import json

from core.shared.logger import get_logger

log = get_logger(__name__)


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _hmac_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, signature_header: str, app_secret: str) -> bool:
    """Header `X-Hub-Signature-256: sha256=<hex>`.

    Fallback: si la firma sobre los bytes crudos no matchea, reintenta sobre
    una re-codificación del JSON con escape de unicode (`ensure_ascii=True`)
    -- Meta escapa unicode al firmar en algunos casos documentados, y ciertos
    proxies/runtimes re-codifican el body en el camino. En FastAPI
    `await request.body()` entrega los bytes exactos recibidos, así que este
    fallback es una red de seguridad barata que se espera que casi nunca
    dispare acá (a diferencia de stacks Node.js, donde sí es un problema
    conocido) -- si dispara, se logea cuál variante funcionó para tener
    datos reales en vez de suponer."""
    if not signature_header.startswith("sha256="):
        return False
    expected_hex = signature_header[len("sha256=") :]

    if hmac.compare_digest(_hmac_hex(app_secret, raw_body), expected_hex):
        return True

    try:
        reencoded = json.dumps(json.loads(raw_body), ensure_ascii=True).encode()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False

    if hmac.compare_digest(_hmac_hex(app_secret, reencoded), expected_hex):
        log.warning("webhook_signature_matched_unicode_escaped_variant")
        return True

    return False
