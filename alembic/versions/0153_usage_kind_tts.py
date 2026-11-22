"""`usage_ledger.kind` admite `'tts'` (HU-BILL-21, ADR-0208 §11).

La voz de salida de pack-voice escribe su fila `kind = 'tts'` con costo y créditos congelados. El
CHECK de 0129 no la admitía. Es la primera de las tres migraciones de SPEC-51 (ADR-0209 §6).

Mismo patrón que 0098 y 0129: el CHECK se recrea `NOT VALID` (no escanea la tabla) y se valida en
el mismo paso con SHARE UPDATE EXCLUSIVE, que no frena escrituras. Aditiva: sin tablas ni columnas
nuevas; la lista anterior es un subconjunto, así que ninguna fila existente la viola.

**Rollback:** primero el código anterior, después el `downgrade`. El downgrade falla si ya hay
filas con `kind = 'tts'` en el ledger (es append-only, ADR-0008): en ese caso basta con volver el
código.

Revision ID: 0153
Revises: 0152
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None

_KINDS = "'turn','agent_run','classifier','composer','embedding','eval','generacion','vision','stt'"


def _kind(kinds: str) -> None:
    op.execute("ALTER TABLE usage_ledger DROP CONSTRAINT IF EXISTS ck_usage_kind")
    op.execute(f"ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_kind CHECK (kind IN ({kinds})) NOT VALID")
    op.execute("ALTER TABLE usage_ledger VALIDATE CONSTRAINT ck_usage_kind")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _kind(f"{_KINDS},'tts'")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _kind(_KINDS)
