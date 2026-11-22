"""`credit_grants`: abonos de créditos por ciclo, acumulado y recargas (ADR-0209 §1, HU-BILL-22).

Tabla de NEGOCIO, solo de inserción como `usage_ledger` (ADR-0008):

- `ciclo` es el **inicio exacto** de la ventana de `ventana_mensual` (`core/billing/periodos.py`) en que
  cuenta el abono. No es el mes: un período reiniciado puede empezar dos ventanas en el mismo mes.
- `tipo`: `acumulado` (sobrante del mes de un plan que acumula, con `vence_en`) o `recarga` (sin
  vencimiento, `vence_en` null). El CHECK `ck_credit_grants_vence_en` ata `vence_en` al tipo.
- `origen`: id del pago de la recarga (`subscription_payments.id`, HU-BILL-23) o inicio del ciclo donde
  nació lo acumulado. Al cerrar un ciclo, lo que queda de cada abono se vuelve a insertar para el
  ciclo siguiente con el mismo `origen`.
- Única por `(tenant_id, ciclo, tipo, origen)`: reintentar el cierre o el webhook no duplica.
- `creditos numeric(12,1)` como `usage_ledger.credits`, con CHECK `> 0`.

Aislamiento: `tenant_id` not null, RLS habilitada y FORZADA con la policy `nullif` en USING y WITH
CHECK, índices que empiezan por `tenant_id`. Inmutable: `prevent_mutation()` (0067) en UPDATE, DELETE y
TRUNCATE, y `app_runtime` solo con SELECT e INSERT (patrón de 0069). Sin `deleted_at`: un abono no se
borra; la única excepción es `purge_tenant` (0072), que descubre las tablas por la columna `tenant_id`
y apaga los triggers con `session_replication_role`.

Aditiva: tabla nueva y vacía, sin locks sobre tablas existentes. El downgrade la borra con sus abonos (como
0143 con `tenant_cron_runs`; el test de 0096 baja por encima de ella): solo para revertir un despliegue recién
hecho, antes de que HU-BILL-23 venda recargas.

**Numeración:** 0154 con `down_revision = "0152"`; la 0153 la usan otras ramas en curso (HU-BILL-21,
`0153_usage_kind_tts`). Se renumera al integrar, para que quede después de la 0153 (ADR-0209 §6).

Revision ID: 0154
Revises: 0152
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "0154"
down_revision = "0152"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE credit_grants (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL,
          ciclo timestamptz NOT NULL,
          tipo text NOT NULL,
          creditos numeric(12, 1) NOT NULL,
          vence_en timestamptz,
          origen text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_credit_grants_tipo CHECK (tipo IN ('acumulado', 'recarga')),
          -- El tope rechaza también NaN (en Postgres `NaN > 0` es verdadero).
          CONSTRAINT ck_credit_grants_creditos CHECK (creditos > 0 AND creditos < 100000000000),
          CONSTRAINT ck_credit_grants_vence_en CHECK (
            (tipo = 'recarga' AND vence_en IS NULL) OR (tipo = 'acumulado' AND vence_en > ciclo)
          ),
          CONSTRAINT ck_credit_grants_origen CHECK (length(origen) BETWEEN 1 AND 200)
        );
        CREATE UNIQUE INDEX uq_credit_grants_ciclo_tipo_origen
          ON credit_grants (tenant_id, ciclo, tipo, origen);
        CREATE INDEX ix_credit_grants_tenant_ciclo ON credit_grants (tenant_id, ciclo);
        """
    )
    op.execute("ALTER TABLE credit_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE credit_grants FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY credit_grants_tenant_isolation ON credit_grants "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    op.execute(
        "CREATE TRIGGER credit_grants_inmutable BEFORE UPDATE OR DELETE ON credit_grants "
        "FOR EACH ROW EXECUTE FUNCTION prevent_mutation()"
    )
    op.execute(
        "CREATE TRIGGER credit_grants_sin_vaciar BEFORE TRUNCATE ON credit_grants "
        "FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation()"
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON credit_grants FROM app_runtime;
                GRANT SELECT, INSERT ON credit_grants TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TRIGGER IF EXISTS credit_grants_sin_vaciar ON credit_grants")
    op.execute("DROP TRIGGER IF EXISTS credit_grants_inmutable ON credit_grants")
    op.execute("DROP POLICY IF EXISTS credit_grants_tenant_isolation ON credit_grants")
    op.execute("DROP TABLE IF EXISTS credit_grants")
