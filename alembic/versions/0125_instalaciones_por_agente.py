"""Habilidades instaladas por agente (ADR-0190 §2, HU-AOS-61).

`pack_installations` (0069) suma el scope `agent`: la habilidad queda instalada y configurada en UN agente del
negocio. La columna `agent_id` va con FK compuesta `(tenant_id, agent_id) → agents (tenant_id, id)`, así una
instalación nunca apunta al agente de otro negocio (ADR-0116 §4). El CHECK de scope pasa a tres casos y el índice
único parcial de las vivas incluye al agente: quitar (borrado lógico) y reinstalar en el mismo agente no choca.

`config_encrypted` ya existe y sigue exigiendo texto cifrado: para `scope='agent'` guarda la config del agente con AAD
`(tenant_id, installation_id)`. Las llaves de Wompi NO van aquí (ADR-0190 §6).

Tabla vacía de filas `agent` al aplicar: no hay backfill (D-68, no hay data real). Los locks: el ALTER toma ACCESS
EXCLUSIVE sobre `pack_installations` (pocas filas: la `core` de secretos y las de conexión) con `lock_timeout`.

Reversible: el downgrade QUITA las filas `agent` (son configuración que se recrea al instalar; la bajada es solo para
rollback y pruebas, y no hay otra forma de volver al CHECK de dos scopes).

Revision ID: 0125
Revises: 0124
Create Date: 2026-11-12
"""

from __future__ import annotations

from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE pack_installations ADD COLUMN agent_id uuid;
        ALTER TABLE pack_installations DROP CONSTRAINT ck_pack_installations_scope;
        ALTER TABLE pack_installations DROP CONSTRAINT IF EXISTS pack_installations_scope_check;
        ALTER TABLE pack_installations ADD CONSTRAINT ck_pack_installations_scope_valor
          CHECK (scope IN ('tenant','connection','agent'));
        ALTER TABLE pack_installations ADD CONSTRAINT ck_pack_installations_scope CHECK (
             (scope = 'tenant'     AND connection_id IS NULL     AND agent_id IS NULL)
          OR (scope = 'connection' AND connection_id IS NOT NULL AND agent_id IS NULL)
          OR (scope = 'agent'      AND connection_id IS NULL     AND agent_id IS NOT NULL));
        ALTER TABLE pack_installations ADD CONSTRAINT fk_pack_installations_agente
          FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id);
        DROP INDEX uq_pack_installations_scope;
        CREATE UNIQUE INDEX uq_pack_installations_scope ON pack_installations (
          tenant_id, pack_id, scope,
          coalesce(connection_id, '00000000-0000-0000-0000-000000000000'::uuid),
          coalesce(agent_id, '00000000-0000-0000-0000-000000000000'::uuid))
          WHERE deleted_at IS NULL;
        CREATE INDEX ix_pack_installations_agente ON pack_installations (tenant_id, agent_id)
          WHERE agent_id IS NOT NULL AND deleted_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM pack_installations WHERE scope = 'agent';
        DROP INDEX ix_pack_installations_agente;
        DROP INDEX uq_pack_installations_scope;
        CREATE UNIQUE INDEX uq_pack_installations_scope ON pack_installations
          (tenant_id, pack_id, scope, coalesce(connection_id, '00000000-0000-0000-0000-000000000000'::uuid))
          WHERE deleted_at IS NULL;
        ALTER TABLE pack_installations DROP CONSTRAINT fk_pack_installations_agente;
        ALTER TABLE pack_installations DROP CONSTRAINT ck_pack_installations_scope;
        ALTER TABLE pack_installations DROP CONSTRAINT ck_pack_installations_scope_valor;
        ALTER TABLE pack_installations ADD CONSTRAINT ck_pack_installations_scope CHECK (
             (scope = 'tenant' AND connection_id IS NULL) OR (scope = 'connection' AND connection_id IS NOT NULL));
        ALTER TABLE pack_installations ADD CONSTRAINT pack_installations_scope_check
          CHECK (scope IN ('tenant','connection'));
        ALTER TABLE pack_installations DROP COLUMN agent_id;
        """
    )
