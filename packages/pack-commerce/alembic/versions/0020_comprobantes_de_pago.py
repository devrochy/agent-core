"""Comprobantes de pago y revisión manual del pago (SPEC-48, HU-COM-22, ADR-0203).

`pack_commerce_payment_proofs` guarda cada comprobante de transferencia que se comprobó: la imagen
(`image_key` en el almacenamiento, `image_sha256`), lo que leyó la visión (`extracted`), el resultado
de la comparación con el pedido (`match`) y el estado (`pagado`, `en_revision`, `rechazado`). Es un
registro del negocio: no se borra (`deleted_at` solo para retirarlo de la vista). La comprobación es
idempotente por `(tenant_id, order_id, image_sha256)`: el índice único parcial lo garantiza.

`pack_commerce_orders` gana tres columnas nullable sin default (solo metadato): quién cambió el
estado del pago a mano, cuándo y por qué. `payment_status` ya es texto libre: `en_revision` no
necesita cambio de esquema (los valores válidos están en el modelo).

Revision ID: crm_0020
Revises: crm_0019
Create Date: 2026-11-17
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "crm_0020"
down_revision = "crm_0019"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)
_TABLE = "pack_commerce_payment_proofs"
_ORDERS = "pack_commerce_orders"
_ESTADOS = "('pagado','en_revision','rechazado')"


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def upgrade() -> None:
    op.execute("set local lock_timeout = '5s'")
    op.create_table(
        _TABLE,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        # UUID lógicos (sin FK): el pedido es de este pack, el mensaje y la conversación del core.
        sa.Column("order_id", _UUID, nullable=False),
        sa.Column("conversation_id", _UUID, nullable=True),
        sa.Column("message_id", _UUID, nullable=True),
        sa.Column("image_key", sa.Text(), nullable=False),
        sa.Column("image_sha256", sa.Text(), nullable=False),
        sa.Column("extracted", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("match", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reviewed_by", _UUID, nullable=True),
        sa.Column("reviewed_at", _TS, nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"status in {_ESTADOS}", name="ck_pack_commerce_payment_proofs_status"),
        sa.CheckConstraint("char_length(image_sha256) = 64", name="ck_pack_commerce_payment_proofs_sha256"),
        sa.CheckConstraint(
            "review_note is null or char_length(review_note) <= 300", name="ck_pack_commerce_payment_proofs_note_len"
        ),
    )
    op.create_index(f"ix_{_TABLE}_tenant_order", _TABLE, ["tenant_id", "order_id", "created_at"])
    op.create_index(f"ix_{_TABLE}_tenant_sha256", _TABLE, ["tenant_id", "image_sha256"])
    op.create_index(
        f"uq_{_TABLE}_tenant_order_sha256",
        _TABLE,
        ["tenant_id", "order_id", "image_sha256"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    _rls(_TABLE)
    op.add_column(_ORDERS, sa.Column("payment_status_changed_by", _UUID, nullable=True))
    op.add_column(_ORDERS, sa.Column("payment_status_changed_at", _TS, nullable=True))
    op.add_column(_ORDERS, sa.Column("payment_status_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    # Reversa solo sin comprobantes: borrar la tabla con evidencia la perdería en silencio.
    conn = op.get_bind()
    conn.execute(sa.text("set local row_security = off"))
    hay = conn.execute(sa.text(f"select exists (select 1 from {_TABLE})")).scalar()
    if hay:
        raise RuntimeError(f"{_TABLE} tiene comprobantes: exportarlos antes de revertir crm_0020.")
    op.drop_column(_ORDERS, "payment_status_reason")
    op.drop_column(_ORDERS, "payment_status_changed_at")
    op.drop_column(_ORDERS, "payment_status_changed_by")
    op.execute(f"drop policy if exists tenant_isolation on {_TABLE}")
    op.execute(f"alter table {_TABLE} no force row level security")
    op.execute(f"alter table {_TABLE} disable row level security")
    op.drop_index(f"uq_{_TABLE}_tenant_order_sha256", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_tenant_sha256", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_tenant_order", table_name=_TABLE)
    op.drop_table(_TABLE)
