"""Envíos de plantillas a un segmento de clientes y consentimiento (HU-CRM-06, ADR-0150).

1. `customer_consents`: consentimiento del cliente para recibir promociones, **solo inserción**
   (la última fila por cliente y propósito manda). Guarda quién y cómo: la Ley 1581 pide poder
   probar la autorización, así que no es un hecho de memoria (que se reemplaza y se olvida).
   - FK compuesta `(tenant_id, customer_id) → customers (tenant_id, id)`.
   - `app_runtime` con SELECT e INSERT: un consentimiento no se edita, se agrega otra fila.
2. `segment_sends`: un envío a un segmento. La vista previa lo crea en `borrador`; confirmar lo
   pasa a `confirmado`; el worker, a `enviando` y `terminado`.
   - `UNIQUE (tenant_id, idempotency_key)` parcial: la misma clave no confirma dos veces.
   - Índice único parcial `(tenant_id, connection_id)` entre los `confirmado`/`enviando`: un solo
     envío en curso por conexión, garantizado por la base.
3. `segment_send_recipients`: la instantánea de la vista previa y la cola durable del envío.
   - `UNIQUE (send_id, customer_id)`; FK compuestas a `segment_sends`, `customers` y
     `conversations`.
   - Índice `(tenant_id, customer_id, created_at DESC)`: regla de días entre promociones y
     línea de tiempo.

Todo con RLS por `app.tenant_id` (FORCE). Sin DELETE (invariante 8).

**Rollback:** primero el código anterior, después el `downgrade` (borra las tablas: los envíos y
los consentimientos se pierden; son registros con valor legal, así que respaldar antes con
`pg_dump -t customer_consents -t segment_sends -t segment_send_recipients` si ya hay datos reales).

Revision ID: 0100
Revises: 0099
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"

_TABLAS = """
CREATE TABLE IF NOT EXISTS customer_consents (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  customer_id uuid NOT NULL,
  proposito text NOT NULL DEFAULT 'promociones',
  estado text NOT NULL,
  origen text NOT NULL,
  evidencia text NOT NULL DEFAULT '',
  actor_admin_id uuid REFERENCES admins (id) ON DELETE SET NULL,
  actor_label text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fk_customer_consents_customer
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  CONSTRAINT ck_customer_consents_estado CHECK (estado IN ('otorgado', 'revocado')),
  CONSTRAINT ck_customer_consents_proposito CHECK (proposito IN ('promociones')),
  CONSTRAINT ck_customer_consents_evidencia CHECK (char_length(evidencia) <= 500)
);
CREATE INDEX IF NOT EXISTS ix_customer_consents_customer
  ON customer_consents (tenant_id, customer_id, proposito, created_at DESC);

CREATE TABLE IF NOT EXISTS segment_sends (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  admin_id uuid REFERENCES admins (id) ON DELETE SET NULL,
  admin_label text NOT NULL DEFAULT '',
  connection_id uuid NOT NULL,
  template_id uuid NOT NULL,
  template_name text NOT NULL,
  template_language text NOT NULL,
  variables jsonb NOT NULL DEFAULT '{}'::jsonb,
  filtro jsonb NOT NULL DEFAULT '{}'::jsonb,
  excluidos jsonb NOT NULL DEFAULT '{}'::jsonb,
  total integer NOT NULL DEFAULT 0,
  estado text NOT NULL DEFAULT 'borrador',
  idempotency_key text,
  request_hash text,
  expires_at timestamptz NOT NULL,
  confirmed_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_segment_sends_tenant_id UNIQUE (tenant_id, id),
  CONSTRAINT fk_segment_sends_connection
    FOREIGN KEY (tenant_id, connection_id) REFERENCES channel_connections (tenant_id, id),
  CONSTRAINT ck_segment_sends_estado CHECK (estado IN ('borrador', 'confirmado', 'enviando', 'terminado')),
  CONSTRAINT ck_segment_sends_total CHECK (total >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_segment_sends_idempotency
  ON segment_sends (tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_segment_sends_en_curso
  ON segment_sends (tenant_id, connection_id) WHERE estado IN ('confirmado', 'enviando');
CREATE INDEX IF NOT EXISTS ix_segment_sends_tenant_created
  ON segment_sends (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS segment_send_recipients (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  send_id uuid NOT NULL,
  customer_id uuid NOT NULL,
  conversation_id uuid NOT NULL,
  estado text NOT NULL DEFAULT 'pendiente',
  motivo text,
  message_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_segment_send_recipients_send_customer UNIQUE (send_id, customer_id),
  CONSTRAINT fk_segment_send_recipients_send
    FOREIGN KEY (tenant_id, send_id) REFERENCES segment_sends (tenant_id, id),
  CONSTRAINT fk_segment_send_recipients_customer
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id),
  CONSTRAINT fk_segment_send_recipients_conversation
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, id),
  CONSTRAINT ck_segment_send_recipients_estado CHECK (estado IN ('pendiente', 'encolado', 'omitido'))
);
CREATE INDEX IF NOT EXISTS ix_segment_send_recipients_send
  ON segment_send_recipients (tenant_id, send_id, estado);
CREATE INDEX IF NOT EXISTS ix_segment_send_recipients_customer
  ON segment_send_recipients (tenant_id, customer_id, created_at DESC);
"""

_GRANTS = {
    "customer_consents": "SELECT, INSERT",
    "segment_sends": "SELECT, INSERT, UPDATE",
    "segment_send_recipients": "SELECT, INSERT, UPDATE",
}


def _rls(tabla: str, grants: str) -> None:
    op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {tabla}_tenant_isolation ON {tabla}")
    op.execute(
        f"CREATE POLICY {tabla}_tenant_isolation ON {tabla} "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON {tabla} FROM app_runtime;
                GRANT {grants} ON {tabla} TO app_runtime;
            END IF;
        END $do$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_TABLAS)
    for tabla, grants in _GRANTS.items():
        _rls(tabla, grants)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS segment_send_recipients")
    op.execute("DROP TABLE IF EXISTS segment_sends")
    op.execute("DROP TABLE IF EXISTS customer_consents")
