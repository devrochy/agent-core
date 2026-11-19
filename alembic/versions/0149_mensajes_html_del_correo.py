"""`messages.html`: el correo lleva su diseño por la cola de salida (ADR-0204).

La confirmación de una cita salía con un HTML genérico porque la cola solo llevaba texto. Para
que conserve su ficha (cita, fecha, hora, negocio) y su botón de calendario sin duplicar el
modelo de envío, el HTML viaja en la misma fila de `messages`.

Columna nullable: sin valor, el comportamiento es el de antes (el canal arma su HTML o sale
solo el texto). Sin cambios de RLS: `messages` ya tiene `tenant_id`, RLS forzada y policy.

Reversible: el downgrade elimina la columna.

Revision ID: 0149
Revises: 0148
Create Date: 2026-11-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("html", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "html")
