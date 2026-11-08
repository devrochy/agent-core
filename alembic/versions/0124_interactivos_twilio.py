"""Botones y listas de WhatsApp vía Twilio (ADR-0186 §9, PR-7i).

En Twilio un mensaje con botones o lista se manda como un Content (`twilio/quick-reply` o `twilio/list-picker`).
Dentro de la ventana de 24 h no necesita aprobación y admite variables en el cuerpo, los títulos y los ids, así que
un solo Content por FORMA (`quick_reply:3`, `list:7`…) sirve para todos los mensajes de esa forma: el cuerpo y los
ids firmados por envío van como `ContentVariables`. `twilio_interactive_contents` cachea ese Content por conexión.

Tabla de NEGOCIO (invariante 1): `tenant_id`, RLS forzada, FK compuesta a la conexión, único `(tenant, conexión,
forma)` entre las vivas. Sin purga: las formas posibles son pocas (hasta 10 de botones y 10 de lista).

Reversible: el downgrade elimina la tabla (los Content quedan en la cuenta de Twilio; se recrean al volver).

Revision ID: 0124
Revises: 0123
Create Date: 2026-11-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_AHORA = sa.text("now()")
_TABLA = "twilio_interactive_contents"


def upgrade() -> None:
    op.create_table(
        _TABLA,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("channel_connection_id", _UUID, nullable=False),
        sa.Column("forma", sa.Text(), nullable=False),
        sa.Column("content_sid", sa.Text(), nullable=False),
        sa.Column("last_used_at", _TS, nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_connection_id"],
            ["channel_connections.tenant_id", "channel_connections.id"],
            name="fk_twilio_interactive_contents_conexion",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "forma ~ '^(quick_reply|list):([1-9]|10)$'", name="ck_twilio_interactive_contents_forma"
        ),
        sa.CheckConstraint(
            "content_sid ~ '^HX[0-9a-f]{32}$'", name="ck_twilio_interactive_contents_sid"
        ),
    )
    op.create_index(
        "uq_twilio_interactive_contents_forma_viva",
        _TABLA,
        ["tenant_id", "channel_connection_id", "forma"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(f"alter table {_TABLA} enable row level security")
    op.execute(f"alter table {_TABLA} force row level security")
    op.execute(
        f"create policy tenant_isolation on {_TABLA} using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute(f"revoke all on {_TABLA} from app_runtime")
    op.execute(f"grant select, insert, update on {_TABLA} to app_runtime")


def downgrade() -> None:
    op.execute(f"drop policy if exists tenant_isolation on {_TABLA}")
    op.execute(f"alter table {_TABLA} no force row level security")
    op.execute(f"alter table {_TABLA} disable row level security")
    op.drop_index("uq_twilio_interactive_contents_forma_viva", table_name=_TABLA)
    op.drop_table(_TABLA)
