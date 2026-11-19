"""Motivo de pérdida y valor estimado del lead (HU-CRM-23, SPEC-33).

`pack_commerce_leads` gana tres columnas nullable sin default (solo metadato en Postgres):
`loss_reason` (catálogo cerrado de cinco motivos), `loss_note` (tope 300) y `estimated_value`
`numeric(12,2)` nunca negativo. `pack_commerce_lead_stage_changes` gana `loss_reason` y `loss_note`:
el cambio a «perdido» guarda el motivo con que se perdió, para que reabrir el lead (que limpia el
motivo vigente) no borre la historia.

Los CHECK se crean `NOT VALID` y luego se validan: sin escaneo bajo el lock exclusivo. Aditiva: un
lead «perdido» anterior queda sin motivo y se muestra como «Sin motivo registrado». No toca datos.

Revision ID: crm_0019
Revises: crm_0018
Create Date: 2026-11-02
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "crm_0019"
down_revision = "crm_0018"
branch_labels = None
depends_on = None

_LEADS = "pack_commerce_leads"
_CAMBIOS = "pack_commerce_lead_stage_changes"
# El catálogo vive en `pack_commerce.etapas.MOTIVOS_DE_PERDIDA`; la migración lo copia a propósito:
# una migración no cambia con el código que la sigue (la prueba de la migración los compara).
_MOTIVOS = "('precio','sin_respuesta','compro_en_otro_lado','no_era_para_el','otro')"

_CHECKS = (
    (_LEADS, "ck_pack_commerce_leads_loss_reason", f"loss_reason is null or loss_reason in {_MOTIVOS}"),
    (_LEADS, "ck_pack_commerce_leads_loss_note_len", "loss_note is null or char_length(loss_note) <= 300"),
    (_LEADS, "ck_pack_commerce_leads_estimated_value", "estimated_value is null or estimated_value >= 0"),
    (_CAMBIOS, "ck_pack_commerce_lead_stage_changes_loss_reason", f"loss_reason is null or loss_reason in {_MOTIVOS}"),
    (_CAMBIOS, "ck_pack_commerce_lead_stage_changes_loss_note_len", "loss_note is null or char_length(loss_note) <= 300"),
)


def upgrade() -> None:
    # Los ALTER piden un lock exclusivo breve: si una transacción larga tiene la tabla, fallar rápido
    # en vez de encolar el tráfico detrás.
    op.execute("set local lock_timeout = '5s'")
    op.add_column(_LEADS, sa.Column("loss_reason", sa.Text(), nullable=True))
    op.add_column(_LEADS, sa.Column("loss_note", sa.Text(), nullable=True))
    op.add_column(_LEADS, sa.Column("estimated_value", sa.Numeric(12, 2), nullable=True))
    op.add_column(_CAMBIOS, sa.Column("loss_reason", sa.Text(), nullable=True))
    op.add_column(_CAMBIOS, sa.Column("loss_note", sa.Text(), nullable=True))
    for tabla, nombre, condicion in _CHECKS:
        op.execute(f"alter table {tabla} add constraint {nombre} check ({condicion}) not valid")
    for tabla, nombre, _ in _CHECKS:
        op.execute(f"alter table {tabla} validate constraint {nombre}")


def downgrade() -> None:
    # Reversa solo si no hay motivos ni valores guardados: quitarlos con datos los perdería en silencio.
    conn = op.get_bind()
    # Las tablas tienen RLS forzada: sin esto los conteos verían 0 filas.
    conn.execute(sa.text("set local row_security = off"))
    con_datos = conn.execute(
        sa.text(
            f"select (select count(*) from {_LEADS} where loss_reason is not null or loss_note is not null "
            f"or estimated_value is not null) + (select count(*) from {_CAMBIOS} where loss_reason is not null "
            "or loss_note is not null)"
        )
    ).scalar()
    if con_datos:
        raise RuntimeError("crm_0019: hay motivos de pérdida o valores guardados; la reversa los borraría.")
    op.execute("set local lock_timeout = '5s'")
    for tabla, nombre, _ in reversed(_CHECKS):
        op.execute(f"alter table {tabla} drop constraint if exists {nombre}")
    op.drop_column(_CAMBIOS, "loss_note")
    op.drop_column(_CAMBIOS, "loss_reason")
    op.drop_column(_LEADS, "estimated_value")
    op.drop_column(_LEADS, "loss_note")
    op.drop_column(_LEADS, "loss_reason")
