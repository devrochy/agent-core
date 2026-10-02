"""AgentSpec desde BD (Sprint 2, ADR-0014) y versión en usage_ledger.

- Tabla `agent_specs`: un AgentSpec por tenant con versionado (ADR-0009-ready:
  UNIQUE(tenant_id, version) + is_active; un solo activo por tenant).
- `usage_ledger.agent_spec_version`: atribución de turnos a versiones (ADR-0009).
- Seed del spec DEFAULT (sin texto de prompts: la identidad/tono/reglas viven en
  playbooks .md referenciados por el índice AgentSpec.playbooks).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-26
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Spec default: estructura del contrato congelado, SIN texto de prompts.
# La identidad/tono/reglas se resuelven vía los playbooks referenciados.
DEFAULT_SPEC_JSON = (
    '{"id":"agent-default","version":"1.0.0","tenantId":"{tenant_id}",'
    '"persona":{"role":"asistente","tone":"cercano","warmth":3,"emojis":"sparse",'
    '"maxLength":500,"forbiddenPhrases":[],"locale":"es-CO"},'
    '"model":{"primary":"deepseek-flash","temperature":0.3},'
    '"memory":{"shortTermTurns":30,"summarize":true},'
    '"playbooks":['
    '{"name":"identidad","description":"Identidad del asistente"},'
    '{"name":"tono","description":"Voz y tono del asistente"},'
    '{"name":"reglas-operacion","description":"Reglas de operación del asistente"}'
    "],"
    '"tools":[],'
    '"policies":{"escalation":{"triggers":["asesor","humano","persona real","reclamo","queja"]},'
    '"confirmBeforeWrite":true,"maxToolCallsPerTurn":10,"budgetUsdPerConversation":0.5},'
    '"packs":[]}'
)


def upgrade() -> None:
    op.create_table(
        "agent_specs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_agent_specs_tenant_version", "agent_specs", ["tenant_id", "version"])
    op.execute(
        "create unique index if not exists uq_agent_specs_active "
        "on agent_specs (tenant_id) where is_active and deleted_at is null"
    )
    op.create_index("ix_agent_specs_tenant_created", "agent_specs", ["tenant_id", "created_at"])

    # RLS + policy (mismo patrón nullif de 0003)
    op.execute("alter table agent_specs enable row level security")
    op.execute(
        "create policy tenant_isolation on agent_specs using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on agent_specs to app_runtime")

    # versión del spec en el ledger para atribuir turnos (ADR-0009)
    op.add_column("usage_ledger", sa.Column("agent_spec_version", sa.Text(), nullable=True))

    # seed del spec default para los tenants existentes que no tengan uno
    bind = op.get_bind()
    tenants = bind.execute(sa.text("select id from tenants where deleted_at is null")).fetchall()
    for (tenant_id,) in tenants:
        already = bind.execute(
            sa.text("select 1 from agent_specs where tenant_id = :t"), {"t": tenant_id}
        ).first()
        if already:
            continue
        spec_json = DEFAULT_SPEC_JSON.replace("{tenant_id}", str(tenant_id))
        bind.execute(
            sa.text(
                "insert into agent_specs (id, tenant_id, version, is_active, spec) "
                "values (gen_random_uuid(), :t, '1.0.0', true, CAST(:spec AS jsonb))"
            ),
            {"t": tenant_id, "spec": spec_json},
        )


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on agent_specs")
    op.execute("alter table agent_specs disable row level security")
    op.execute("drop index if exists uq_agent_specs_active")
    op.execute("drop index if exists ix_agent_specs_tenant_created")
    op.drop_column("usage_ledger", "agent_spec_version")
    op.drop_table("agent_specs")