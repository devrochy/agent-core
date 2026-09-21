"""meta_tokens.kind: agrega 'webhook_verify_token' al CHECK.

Wizard BYOK de WhatsApp (`core/gateway/meta_byok.py`): el admin puede
generar o escribir un verify_token PROPIO para el webhook de su app en
vez de depender siempre de `settings.whatsapp_verify_token` (global) --
se guarda cifrado con este `kind` dedicado.

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-20
"""
from __future__ import annotations

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "'user_access','system_user','page_access','long_lived_user',"
    "'app_access','business_integration_system_user','phone_register_pin',"
    "'cloud_api_access_token'"
)
_NEW_KINDS = _OLD_KINDS + ",'webhook_verify_token'"


def upgrade() -> None:
    op.execute("alter table meta_private.meta_tokens drop constraint ck_meta_tokens_kind")
    op.execute(f"alter table meta_private.meta_tokens add constraint ck_meta_tokens_kind check (kind in ({_NEW_KINDS}))")


def downgrade() -> None:
    op.execute("alter table meta_private.meta_tokens drop constraint ck_meta_tokens_kind")
    op.execute(f"alter table meta_private.meta_tokens add constraint ck_meta_tokens_kind check (kind in ({_OLD_KINDS}))")
