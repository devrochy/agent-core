"""Plantillas de WhatsApp por evento (HU-ARCH-13, ADR-0108).

- `app_settings.message_templates_map`: por negocio, evento → plantilla aprobada,
  idioma, variables (rutas del contexto del evento) y texto de respaldo.
- `messages.template_name/template_language/template_components`: un outbound que
  se envía como plantilla (fuera de la ventana de 24 h). `template_components` es el
  payload de Graph ya resuelto (variables del evento o de la BD, nunca del LLM).

Columnas nuevas nullables (o con default constante): sin reescritura de tabla.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "message_templates_map",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("messages", sa.Column("template_name", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("template_language", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("template_components", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "template_components")
    op.drop_column("messages", "template_language")
    op.drop_column("messages", "template_name")
    op.drop_column("app_settings", "message_templates_map")
