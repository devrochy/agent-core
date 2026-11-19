"""Veredictos del gate de pass^k atados a la release candidata (HU-AOS-21, adenda de ADR-0117).

Aditiva y nula, como dejó dicho la 0070:

- `eval_runs.release_id`: la release que se midió, con FK compuesta `(tenant_id, release_id)` →
  `connection_releases (tenant_id, id)` (una corrida nunca apunta a la release de otro negocio).
- `eval_runs.origen`: `gate`, `manual` o `nightly` (texto libre, sin CHECK: lo valida quien escribe).
- `eval_results.procedure_id`: el procedimiento que mide cada escenario.

La FK entra `NOT VALID` y se valida en la misma migración: las filas viejas tienen `release_id`
nulo y no la violan. Las tablas son chicas (una fila por corrida o escenario).

Revision ID: 0105
Revises: 0104
Create Date: 2026-10-25
"""

from __future__ import annotations

from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS release_id uuid")
    op.execute("ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS origen text")
    op.execute("ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS procedure_id text")
    op.execute(
        "ALTER TABLE eval_runs ADD CONSTRAINT fk_eval_runs_release "
        "FOREIGN KEY (tenant_id, release_id) REFERENCES connection_releases (tenant_id, id) NOT VALID"
    )
    op.execute("ALTER TABLE eval_runs VALIDATE CONSTRAINT fk_eval_runs_release")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_runs_tenant_release "
        "ON eval_runs (tenant_id, release_id, started_at DESC) WHERE release_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_eval_results_tenant_run_proc "
        "ON eval_results (tenant_id, run_id, procedure_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_eval_results_tenant_run_proc")
    op.execute("DROP INDEX IF EXISTS ix_eval_runs_tenant_release")
    op.execute("ALTER TABLE eval_runs DROP CONSTRAINT IF EXISTS fk_eval_runs_release")
    op.execute("ALTER TABLE eval_results DROP COLUMN IF EXISTS procedure_id")
    op.execute("ALTER TABLE eval_runs DROP COLUMN IF EXISTS origen")
    op.execute("ALTER TABLE eval_runs DROP COLUMN IF EXISTS release_id")
