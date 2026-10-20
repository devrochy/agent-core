"""Tipo de cita: `color` (paleta categórica) y `codigo` (identificador corto).

HU-UX-08 (SPEC-12): la Agenda pinta cada cita con el color y el código de su
tipo. `color` es una clave de la paleta categórica del sistema de diseño
(`cat-1`…`cat-4`, SPEC-29), nunca un hex libre: el CHECK lo garantiza. `codigo`
es un texto corto (máx. 6) como «VAL».

Las filas existentes reciben un color rotando la paleta por negocio (en orden de
nombre) y como código las 3 primeras letras del nombre, para que la semana ya se
distinga sin que el dueño edite cada tipo. Tabla chica: los `ALTER` toman
`ACCESS EXCLUSIVE` unos milisegundos (`lock_timeout` evita la cola). Columnas con default: el downgrade
solo las quita.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_TABLE = "pack_calendar_appointment_types"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(_TABLE, sa.Column("color", sa.Text(), nullable=False, server_default="cat-1"))
    op.add_column(_TABLE, sa.Column("codigo", sa.Text(), nullable=False, server_default=""))
    op.create_check_constraint(
        "ck_pack_calendar_types_color", _TABLE, "color in ('cat-1', 'cat-2', 'cat-3', 'cat-4')"
    )
    op.create_check_constraint(
        "ck_pack_calendar_types_codigo", _TABLE, "char_length(codigo) <= 6"
    )
    # Backfill sin tenant_scope: con RLS forzada solo toca filas si el rol de
    # migraciones tiene BYPASSRLS (ADR-0094 §5; test y producción lo tienen). Si no,
    # actualiza 0 filas y quedan los defaults: el panel deriva el código del nombre.
    op.execute(
        f"""
        update {_TABLE} t set
          color = 'cat-' || (((o.n - 1) % 4) + 1)::text,
          codigo = upper(left(regexp_replace(t.name, '[^[:alnum:]]', '', 'g'), 3))
        from (
          select id, row_number() over (partition by tenant_id order by name, created_at) as n
          from {_TABLE}
        ) o
        where o.id = t.id
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_pack_calendar_types_codigo", _TABLE, type_="check")
    op.drop_constraint("ck_pack_calendar_types_color", _TABLE, type_="check")
    op.drop_column(_TABLE, "codigo")
    op.drop_column(_TABLE, "color")
