"""Notas del cliente y escalaciones por conversación (HU-CRM-04, ADR-0147).

1. `customer_notes`: notas que una persona del equipo deja sobre un cliente (no sobre una
   conversación). Las ve el equipo en la ficha y el agente en el bloque dinámico del prompt.
   - FK compuesta `(tenant_id, customer_id) → customers (tenant_id, id)` (patrón de 0067/0069): una
     nota no puede colgar de un cliente de otro negocio.
   - `author_admin_id → admins` con `ON DELETE SET NULL` (`admins` es global) y `author_label`
     congelado al escribir.
   - RLS por `app.tenant_id` (FORCE); `app_runtime` con SELECT, INSERT y UPDATE. Borrar una nota
     o purgar la memoria del cliente marca `deleted_at` (invariante 8). Sin DELETE.
   - Índice parcial `(tenant_id, customer_id, created_at DESC)` entre las vivas: ficha, línea de
     tiempo y prompt leen por cliente, de la más nueva a la más vieja.
2. Índice `escalations (tenant_id, conversation_id, created_at)`: la línea de tiempo trae las
   escalaciones de las conversaciones de un cliente; hoy solo existe `(tenant_id, status)`.

**Rollback:** primero el código anterior, después el `downgrade` (borra la tabla: las notas se
pierden; respaldar antes si ya hay notas reales).

Revision ID: 0099
Revises: 0098
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"

_TABLA = """
CREATE TABLE IF NOT EXISTS customer_notes (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  customer_id uuid NOT NULL,
  text text NOT NULL,
  author_admin_id uuid REFERENCES admins (id) ON DELETE SET NULL,
  author_label text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  CONSTRAINT fk_customer_notes_customer
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  CONSTRAINT ck_customer_notes_text CHECK (char_length(btrim(text)) BETWEEN 1 AND 2000)
);
CREATE INDEX IF NOT EXISTS ix_customer_notes_customer
  ON customer_notes (tenant_id, customer_id, created_at DESC) WHERE deleted_at IS NULL;
"""


def _rls() -> None:
    op.execute("ALTER TABLE customer_notes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE customer_notes FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS customer_notes_tenant_isolation ON customer_notes")
    op.execute(
        "CREATE POLICY customer_notes_tenant_isolation ON customer_notes "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON customer_notes FROM app_runtime;
                GRANT SELECT, INSERT, UPDATE ON customer_notes TO app_runtime;
            END IF;
        END $do$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_TABLA)
    _rls()
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_escalations_tenant_conversation "
        "ON escalations (tenant_id, conversation_id, created_at)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_escalations_tenant_conversation")
    op.execute("DROP TABLE IF EXISTS customer_notes")
