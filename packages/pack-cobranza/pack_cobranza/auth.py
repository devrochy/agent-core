"""Auth de gestores del pack (ADR-0057): JWT HS256 propio, sin dependencia externa.

El token lleva `tenant_id`, `gestor_id` y `device_id`; el secreto vive en la
config del pack (`CobranzaConfig.jwt_secret`). El pack autentica cada request de
su API pública; el core no conoce a los usuarios de campo.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

PLUGIN_ID = "pack_cobranza"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(payload: dict[str, Any], secret: str, ttl_hours: int = 12) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    body = {**payload, "exp": int(time.time()) + ttl_hours * 3600}
    seg = (
        _b64e(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64e(json.dumps(body, separators=(",", ":")).encode())
    )
    sig = hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
    return f"{seg}.{_b64e(sig)}"


def verify_token(token: str, secret: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_seg, body_seg, sig_seg = parts
    expected = _b64e(hmac.new(secret.encode(), f"{header_seg}.{body_seg}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig_seg):
        return None
    try:
        payload = json.loads(_b64d(body_seg))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload

async def pack_secret() -> str | None:
    """Secreto de firma del pack (config del tenant activo).

    Si aún no existe, lo **autogenera y persiste** (primer uso de la API de
    campo), para que la app funcione sin configuración manual previa.
    """
    import secrets

    from core.plugin_manager import plugin_manager
    from pack_cobranza.config import CobranzaConfig

    try:
        cfg = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        return None
    if isinstance(cfg, CobranzaConfig) and cfg.jwt_secret:
        value = cfg.jwt_secret.get_secret_value()
        if value:
            return value

    new_secret = secrets.token_urlsafe(48)
    base = cfg if isinstance(cfg, CobranzaConfig) else CobranzaConfig()
    values: dict[str, Any] = {}
    for name in CobranzaConfig.model_fields:
        val = getattr(base, name, None)
        values[name] = val.get_secret_value() if hasattr(val, "get_secret_value") else val
    values["jwt_secret"] = new_secret
    try:
        await plugin_manager.save_config(PLUGIN_ID, values)
    except Exception:
        from core.shared.logger import get_logger

        get_logger(__name__).warning("pack_secret_generate_failed", exc_info=True)
        return None
    return new_secret


async def token_tenant(token: str) -> tuple[uuid.UUID, dict[str, Any]] | None:
    """Resuelve (tenant_id, payload) de un token; None si es inválido.

    El `tenant_id` va en el payload (el token lo emitió el propio pack), así que
    se valida la firma contra el secreto de ESE tenant.
    """
    from core.shared.tenant import tenant_scope

    # Primer intento: leer el tenant del payload sin verificar (para cargar su config).
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = json.loads(_b64d(parts[1]))
        tenant_id = uuid.UUID(str(raw.get("tenant_id")))
    except Exception:
        return None
    async with tenant_scope(tenant_id):
        secret = await pack_secret()
    if not secret:
        return None
    payload = verify_token(token, secret)
    if payload is None:
        return None
    return tenant_id, payload
