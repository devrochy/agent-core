"""Foto de producto subida al almacenamiento propio (HU-CRM-05, SPEC-33).

`pack_commerce_products.image_key` guarda la clave del controlador único
(`TENANTS_ROOT/<slug>/productos/...`, ADR-0085 enmendado) cuando la foto se sube desde el
panel o la APK. `image_url` sigue siendo el link externo (HU-CRM-01); si hay `image_key`,
gana y la API/agente devuelven una URL firmada.

`add column` nullable sin default es solo metadato en Postgres (sin reescritura).

Revision ID: crm_0013
Revises: crm_0012
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op

revision = "crm_0013"
down_revision = "crm_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("set local lock_timeout = '5s'")
    op.execute("alter table pack_commerce_products add column if not exists image_key text")


def downgrade() -> None:
    op.execute("set local lock_timeout = '5s'")
    op.execute("alter table pack_commerce_products drop column if exists image_key")
