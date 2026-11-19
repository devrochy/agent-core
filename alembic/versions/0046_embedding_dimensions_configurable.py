"""memory_vectors.embedding respeta EMBEDDING_DIMENSIONS (HU-ARCH-24).

La columna estaba fija en `vector(384)` y desconectada de la config
`EMBEDDING_DIMENSIONS`. Esta migración la re-ajusta a la dimensión configurada.
Si la tabla tiene filas, cambiar la dimensión requiere reindexar; se aborta con
un mensaje claro en vez de corromper la memoria semántica.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-03
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from core.config import get_settings

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dim = get_settings().embedding_dimensions
    conn = op.get_bind()
    tiene_filas = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM memory_vectors LIMIT 1)")
    ).scalar()
    actual = conn.execute(
        sa.text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'memory_vectors'::regclass AND attname = 'embedding'"
        )
    ).scalar()
    # atttypmod de pgvector guarda la dimensión directamente.
    if actual == dim:
        return
    if tiene_filas:
        raise RuntimeError(
            f"memory_vectors tiene filas y su embedding es de dim {actual}; "
            f"cambiar a {dim} requiere reindexar. Migra los datos primero."
        )
    op.execute(f"ALTER TABLE memory_vectors ALTER COLUMN embedding TYPE vector({dim})")


def downgrade() -> None:
    op.execute("ALTER TABLE memory_vectors ALTER COLUMN embedding TYPE vector(384)")
