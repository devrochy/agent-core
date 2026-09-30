"""meta_tokens.kind: agrega 'cloud_api_access_token' al CHECK.

Wizard BYOK de WhatsApp (`core/gateway/meta_byok.py`): el admin pega a mano
el access token temporal que Meta da en la pestaña "API Setup" de su propia
app -- se guarda cifrado con este `kind` dedicado, distinto de
`business_integration_system_user` (que es semánticamente el token que
emite el flujo OAuth de Embedded Signup, no uno pegado a mano).

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-20
"""
from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "'user_access','system_user','page_access','long_lived_user',"
    "'app_access','business_integration_system_user','phone_register_pin'"
)
_NEW_KINDS = _OLD_KINDS + ",'cloud_api_access_token'"


def upgrade() -> None:
    op.execute("alter table meta_private.meta_tokens drop constraint ck_meta_tokens_kind")
    op.execute(f"alter table meta_private.meta_tokens add constraint ck_meta_tokens_kind check (kind in ({_NEW_KINDS}))")


def downgrade() -> None:
    op.execute("alter table meta_private.meta_tokens drop constraint ck_meta_tokens_kind")
    op.execute(f"alter table meta_private.meta_tokens add constraint ck_meta_tokens_kind check (kind in ({_OLD_KINDS}))")
