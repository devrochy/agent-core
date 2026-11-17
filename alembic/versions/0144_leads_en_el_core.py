"""Los leads viven en el core (ADR-0193, HU-AOS-70, PR 1).

Cuatro tablas de NEGOCIO, todas aditivas y sin escritores todavía (el cambio de dueño es el PR 2):

1. `leads`: el enriquecimiento de embudo del `Customer` (ADR-0022), con las columnas que hoy tiene
   `pack_commerce_leads` más `origen` (catálogo cerrado), `custom` (existe en commerce desde crm_0003 aunque su ORM
   no la declare; se conserva para no perder datos al copiar), `updated_at` y `deleted_at`. `status` sigue sin CHECK:
   commerce guarda texto libre de leads anteriores a HU-CRM-18 y la copia no puede fallar por eso.
   Únicos parciales entre los vivos: `(tenant_id, idempotency_key)` y `(tenant_id, customer_id)` (asegurar el lead,
   ❓3 de HU-AOS-70); los leads sin cliente no se deduplican.
2. `lead_stage_changes`: historia de etapa con actor (HU-CRM-18 y 23). `from_stage` sin CHECK por la misma razón.
3. `lead_followups`: seguimientos cerrados (HU-CRM-21).
4. `lead_settings`: la configuración de Captar leads del negocio (ADR-0193 §6), una fila por negocio, cifrada con
   la caja de secretos como hoy en `plugin_configs`. Sin fila viva, Captar leads no está configurado.

`lead_id` y `customer_id` quedan como FK lógicas, como en commerce: una FK real haría fallar la copia con los
huérfanos que commerce pueda tener (nunca las tuvo). La integridad la dan el servicio de leads y el orden de la copia.

Las cuatro con `tenant_id`, RLS habilitada y forzada, policy de aislamiento, índices que empiezan por `tenant_id` y
sin DELETE para `app_runtime` (invariante 8). `purge_tenant` las descubre por su `tenant_id` (0011/0072).

Los catálogos de etapas, actores y motivos se copian aquí a propósito: una migración no cambia con el código que la
sigue (el test de la migración los compara con `core.leads.catalogo`).

Reversible: el downgrade borra las cuatro tablas, como 0121 y 0137. Mientras commerce siga siendo el dueño (hasta la
parte 2) no se pierde nada: los originales siguen en `pack_commerce_leads` y `agent-core db copiar-leads` los vuelve
a copiar. Después del cambio de dueño, bajar esta migración pierde lo que el core haya escrito.

Revision ID: 0144
Revises: 0143
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_AHORA = sa.text("now()")

_LEADS = "leads"
_CAMBIOS = "lead_stage_changes"
_SEGUIMIENTOS = "lead_followups"
_AJUSTES = "lead_settings"
_TABLAS = (_LEADS, _CAMBIOS, _SEGUIMIENTOS, _AJUSTES)

_ETAPAS = "('nuevo','calificado','contactado','convertido','perdido')"
_ORIGENES = "('landing','chat_web','whatsapp','instagram','telegram','manual')"
_MOTIVOS = "('precio','sin_respuesta','compro_en_otro_lado','no_era_para_el','otro')"
_VIVO = "deleted_at is null"


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute(f"revoke all on {table} from app_runtime")
    op.execute(f"grant select, insert, update on {table} to app_runtime")


def upgrade() -> None:
    op.create_table(
        _LEADS,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("customer_id", _UUID, nullable=True),  # FK lógica a customers
        sa.Column("conversation_id", _UUID, nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("interest", sa.Text(), nullable=False, server_default=""),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="nuevo"),
        sa.Column("origen", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("custom", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_activity_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("stage_changed_at", _TS, nullable=True),
        sa.Column("next_action_at", _TS, nullable=True),
        sa.Column("next_action_note", sa.Text(), nullable=True),
        sa.Column("loss_reason", sa.Text(), nullable=True),
        sa.Column("loss_note", sa.Text(), nullable=True),
        sa.Column("estimated_value", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.CheckConstraint(f"origen is null or origen in {_ORIGENES}", name="ck_leads_origen"),
        sa.CheckConstraint(
            "next_action_note is null or char_length(next_action_note) <= 200", name="ck_leads_next_action_note_len"
        ),
        sa.CheckConstraint(f"loss_reason is null or loss_reason in {_MOTIVOS}", name="ck_leads_loss_reason"),
        sa.CheckConstraint("loss_note is null or char_length(loss_note) <= 300", name="ck_leads_loss_note_len"),
        sa.CheckConstraint("estimated_value is null or estimated_value >= 0", name="ck_leads_estimated_value"),
    )
    op.create_index(
        "uq_leads_tenant_idem",
        _LEADS,
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text(f"idempotency_key is not null and {_VIVO}"),
    )
    # Un lead vivo por cliente: asegurar el lead es idempotente (ADR-0193 §2).
    op.create_index(
        "uq_leads_tenant_cliente",
        _LEADS,
        ["tenant_id", "customer_id"],
        unique=True,
        postgresql_where=sa.text(f"customer_id is not null and {_VIVO}"),
    )
    op.create_index(
        "ix_leads_etapa", _LEADS, ["tenant_id", "status", "last_activity_at"], postgresql_where=sa.text(_VIVO)
    )
    # Recordatorios de seguimiento (cron) y filtro «vence hoy» de Clientes.
    op.create_index(
        "ix_leads_seguimiento",
        _LEADS,
        ["tenant_id", "next_action_at"],
        postgresql_where=sa.text(f"next_action_at is not null and {_VIVO}"),
    )
    _rls(_LEADS)

    op.create_table(
        _CAMBIOS,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("lead_id", _UUID, nullable=False),  # FK lógica a leads
        sa.Column("customer_id", _UUID, nullable=True),
        sa.Column("from_stage", sa.Text(), nullable=False),
        sa.Column("to_stage", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("changed_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("loss_reason", sa.Text(), nullable=True),
        sa.Column("loss_note", sa.Text(), nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.CheckConstraint(f"to_stage in {_ETAPAS}", name="ck_lead_stage_changes_to_stage"),
        sa.CheckConstraint("actor in ('agente','persona','sistema')", name="ck_lead_stage_changes_actor"),
        sa.CheckConstraint(
            f"loss_reason is null or loss_reason in {_MOTIVOS}", name="ck_lead_stage_changes_loss_reason"
        ),
        sa.CheckConstraint(
            "loss_note is null or char_length(loss_note) <= 300", name="ck_lead_stage_changes_loss_note_len"
        ),
    )
    op.create_index(
        "uq_lead_stage_changes_tenant_idem",
        _CAMBIOS,
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text(f"idempotency_key is not null and {_VIVO}"),
    )
    # Línea de tiempo de la ficha: los cambios del cliente, del más nuevo al más viejo.
    op.create_index("ix_lead_stage_changes_cliente", _CAMBIOS, ["tenant_id", "customer_id", "changed_at", "id"])
    op.create_index("ix_lead_stage_changes_lead", _CAMBIOS, ["tenant_id", "lead_id"])
    _rls(_CAMBIOS)

    op.create_table(
        _SEGUIMIENTOS,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("lead_id", _UUID, nullable=False),  # FK lógica a leads
        sa.Column("customer_id", _UUID, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("due_at", _TS, nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False, server_default="persona"),
        sa.Column("closed_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.CheckConstraint("outcome in ('hecho','reprogramado','cancelado')", name="ck_lead_followups_outcome"),
        sa.CheckConstraint("actor in ('persona','sistema')", name="ck_lead_followups_actor"),
    )
    op.create_index("ix_lead_followups_cliente", _SEGUIMIENTOS, ["tenant_id", "customer_id", "closed_at", "id"])
    op.create_index("ix_lead_followups_lead", _SEGUIMIENTOS, ["tenant_id", "lead_id"])
    _rls(_SEGUIMIENTOS)

    op.create_table(
        _AJUSTES,
        sa.Column("tenant_id", _UUID, primary_key=True),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("deleted_at", _TS, nullable=True),
    )
    _rls(_AJUSTES)


def downgrade() -> None:
    for tabla in reversed(_TABLAS):
        op.drop_table(tabla)
