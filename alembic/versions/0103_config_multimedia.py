"""Configuración multimedia por negocio (SPEC-04, ADR-0151).

Agrega a `app_settings` el proveedor/modelo/llave de audio (STT) y de visión, más
los interruptores de habilitado y el modo de imagen (`describir`/`sin_descripcion`).
Las llaves se guardan cifradas; el fallback de plataforma vive en el entorno.

**Rollback:** solo el `downgrade`, sin datos que migrar.

Revision ID: 0103
Revises: 0102
Create Date: 2026-10-22
"""

from __future__ import annotations

from alembic import op

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS audio_enabled boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS audio_provider text")
    op.execute("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS audio_model text")
    op.execute("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS audio_api_key_encrypted text")
    op.execute("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS vision_enabled boolean NOT NULL DEFAULT true")
    op.execute(
        "ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS vision_mode text NOT NULL DEFAULT 'describir'"
    )
    op.execute("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS vision_provider text")
    op.execute("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS vision_model text")
    op.execute("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS vision_api_key_encrypted text")
    op.execute(
        "ALTER TABLE app_settings ADD CONSTRAINT ck_app_settings_vision_mode "
        "CHECK (vision_mode in ('describir','sin_descripcion'))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE app_settings DROP CONSTRAINT IF EXISTS ck_app_settings_vision_mode")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS vision_api_key_encrypted")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS vision_model")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS vision_provider")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS vision_mode")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS vision_enabled")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS audio_api_key_encrypted")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS audio_model")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS audio_provider")
    op.execute("ALTER TABLE app_settings DROP COLUMN IF EXISTS audio_enabled")
