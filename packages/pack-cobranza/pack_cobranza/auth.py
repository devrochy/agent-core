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

from sqlalchemy import select

from core.admin.roles import actua_como_master, cuenta_habilitada
from core.db.models import Admin, AdminPermission, TenantUser
from core.db.session import get_session
from core.tenants import is_tenant_active

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


async def _vinculado(session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID) -> bool:
    """¿La cuenta sigue teniendo el negocio? Por `tenant_user` o, como la rama `global` de
    `admin_tenants()` (0035/0088), por un permiso global (`admin_permissions.tenant_id` null),
    que el login de campo trata como `master`. Tablas de sistema, sin RLS."""
    row = (
        await session.execute(
            select(TenantUser.id).where(
                TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return True
    global_ = (
        await session.execute(
            select(AdminPermission.id)
            .where(AdminPermission.admin_id == admin_id, AdminPermission.tenant_id.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    return global_ is not None


async def acceso_de_campo(
    authorization: str | None, *, sin_equipo_ok: bool = False
) -> tuple[uuid.UUID, dict[str, Any]] | None:
    """Único validador de los tokens de acceso de la app de campo (`api.py` y `api_gestor.py`).

    Firma y vencimiento (`token_tenant`), y además, en CADA pedido (ADR-0134, ADR-0136):
    - el refresh (30 días) y el token de membresía no sirven como acceso;
    - el negocio sigue activo (suspenderlo corta la app en el siguiente pedido);
    - si el token es de una cuenta (`admin_id`): la cuenta sigue habilitada (desactivarla o
      apagar el rol tester corta la app) y sigue vinculada al negocio por `tenant_user`
      (desvincularla también); el master y el tester habilitado no necesitan vínculo;
    - si es de un gestor: el equipo con el que entró (`did`) sigue activo (ADR-0174): bloquearlo corta la
      sesión abierta con 403 `code` (`AccesoError`) en menos de 60 s. `sin_equipo_ok` deja pasar un token
      sin `did` en las rutas por las que un equipo pide vincularse.
    Los tokens de dispositivo (sin `admin_id`, `/auth/dispositivo`) solo dependen del negocio.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    auth = await token_tenant(authorization.split(" ", 1)[1].strip())
    if auth is None:
        return None
    tenant_id, payload = auth
    if payload.get("typ") in ("refresh", "membresia"):
        return None
    if not await is_tenant_active(tenant_id):
        return None
    admin_id = payload.get("admin_id")
    if admin_id:
        try:
            admin_uuid = uuid.UUID(str(admin_id))
        except (TypeError, ValueError):
            return None
        async with get_session() as session:
            admin = await session.get(Admin, admin_uuid)
            if not cuenta_habilitada(admin):
                return None
            if not actua_como_master(admin) and not await _vinculado(session, tenant_id, admin_uuid):
                return None
        if payload.get("rol") == "gestor" and not actua_como_master(admin):
            from core.access.sesion import exigir_equipo_vigente
            from core.shared.tenant import tenant_scope

            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    await exigir_equipo_vigente(
                        session, tenant_id, admin_uuid, payload.get("did"), sin_equipo_ok=sin_equipo_ok
                    )
    return auth
