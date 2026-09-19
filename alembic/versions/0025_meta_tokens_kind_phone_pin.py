"""meta_tokens.kind: agrega 'phone_register_pin' al CHECK.

El PIN de 6 dígitos generado para `POST /{phone_number_id}/register` (core/
gateway/meta_onboarding.py::register_number_step) se guarda cifrado en
`meta_private.meta_tokens` con este `kind` dedicado -- mismo mecanismo de
custodia que el token de la conexión, nunca en texto plano ni en
`connection_wizards`.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-22
"""
from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "'user_access','system_user','page_access','long_lived_user',"
    "'app_access','business_integration_system_user'"
)
_NEW_KINDS = _OLD_KINDS + ",'phone_register_pin'"


def upgrade() -> None:
    op.execute("alter table meta_private.meta_tokens drop constraint ck_meta_tokens_kind")
    op.execute(f"alter table meta_private.meta_tokens add constraint ck_meta_tokens_kind check (kind in ({_NEW_KINDS}))")


def downgrade() -> None:
    op.execute("alter table meta_private.meta_tokens drop constraint ck_meta_tokens_kind")
    op.execute(f"alter table meta_private.meta_tokens add constraint ck_meta_tokens_kind check (kind in ({_OLD_KINDS}))")
