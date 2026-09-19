"""graph_api_calls: columnas de detalle del error (method, error_subcode,
error_type, fbtrace_id, request_id) para el cliente único de Graph API del
panel. ALTER TABLE sin backfill -- la tabla está vacía, nada la usa todavía.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-19
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("graph_api_calls", sa.Column("method", sa.Text(), nullable=True))
    op.add_column("graph_api_calls", sa.Column("error_subcode", sa.Integer(), nullable=True))
    op.add_column("graph_api_calls", sa.Column("error_type", sa.Text(), nullable=True))
    op.add_column("graph_api_calls", sa.Column("fbtrace_id", sa.Text(), nullable=True))
    op.add_column("graph_api_calls", sa.Column("request_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("graph_api_calls", "request_id")
    op.drop_column("graph_api_calls", "fbtrace_id")
    op.drop_column("graph_api_calls", "error_type")
    op.drop_column("graph_api_calls", "error_subcode")
    op.drop_column("graph_api_calls", "method")
