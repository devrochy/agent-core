"""Config de la App de Meta por tenant (BYOK), para el preflight de integración.

Separado de `channel_connections` (core/gateway/connections.py) a propósito:
una app de Meta es un singleton por tenant (como `AppSettings`), mientras que
`ChannelConnection` es por número de WhatsApp -- un tenant puede tener varios
números bajo la MISMA app. `app_id`/`config_id` no son secretos (aparecen en
URLs y en la consola de Meta sin restricción); `app_secret` sí. Se cifran los
tres juntos en un solo blob por simplicidad -- ya que cualquier parte es
secreta, todo el blob debe estar cifrado igual.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator
from sqlalchemy import select

from core.config import get_settings
from core.db.models import AppSettings
from core.db.session import get_session
from core.shared.crypto import get_secret_box
from core.shared.tenant import get_current_tenant_id

_NUMERIC_ID = re.compile(r"^\d+$")


class MetaAppConfig(BaseModel):
    app_id: str
    app_secret: str
    config_id: str

    @field_validator("app_id", "config_id")
    @classmethod
    def _debe_ser_numerico(cls, value: str) -> str:
        if not _NUMERIC_ID.fullmatch(value or ""):
            raise ValueError("debe ser un ID numérico de Meta (solo dígitos)")
        return value

    @field_validator("app_secret")
    @classmethod
    def _no_vacio(cls, value: str) -> str:
        if not value:
            raise ValueError("no puede estar vacío")
        return value


async def load_meta_app_config(tenant_id=None) -> MetaAppConfig | None:
    """Config guardada en BD para el tenant. None si no configuró nada todavía."""
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    async with get_session() as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.tenant_id == tid))
        ).scalar_one_or_none()
    if row is None or not row.meta_app_config_encrypted:
        return None
    raw = get_secret_box().decrypt_json(row.meta_app_config_encrypted)
    try:
        return MetaAppConfig.model_validate(raw)
    except Exception:
        return None


async def save_meta_app_config(config: MetaAppConfig, tenant_id=None) -> None:
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    encrypted = get_secret_box().encrypt_json(config.model_dump())
    async with get_session() as session:
        row = (
            await session.execute(select(AppSettings).where(AppSettings.tenant_id == tid))
        ).scalar_one_or_none()
        if row is None:
            row = AppSettings(tenant_id=tid, meta_app_config_encrypted=encrypted)
            session.add(row)
        else:
            row.meta_app_config_encrypted = encrypted


def _env_meta_app_config() -> MetaAppConfig | None:
    """Fallback opcional de dev/single-tenant (META_APP_ID/SECRET/CONFIG_ID en .env)."""
    settings = get_settings()
    try:
        return MetaAppConfig(
            app_id=settings.meta_app_id,
            app_secret=settings.meta_app_secret,
            config_id=settings.meta_config_id,
        )
    except Exception:
        return None


async def effective_meta_app_config(tenant_id=None) -> MetaAppConfig | None:
    """BD del tenant -> fallback de .env -> None (nunca levanta excepción)."""
    config = await load_meta_app_config(tenant_id)
    if config is not None:
        return config
    return _env_meta_app_config()
