"""`usage_ledger.kind` admite `'vision'` y `'stt'` (HU-BILL-16, ADR-0194).

Las fotos del catálogo se cobran con una fila `kind = 'vision'` con créditos reales, y la
idempotencia por foto lee `result->>'image_key'` de esas filas. El CHECK de 0098 no admitía ni
`'vision'` ni `'stt'`: la ingesta de WhatsApp ya intentaba escribir sus filas en sombra de imágenes
y notas de voz (`credits = 0`) y fallaba en silencio (`media_ledger_failed`). Desde aquí esas filas
en sombra también quedan registradas.

Mismo patrón que 0098: el CHECK se recrea `NOT VALID` (no escanea la tabla) y se valida en el
mismo paso con SHARE UPDATE EXCLUSIVE, que no frena escrituras. Sin tablas ni columnas nuevas.

**Rollback:** primero el código anterior, después el `downgrade`. El downgrade falla si ya hay
filas con `kind = 'vision'` o `'stt'` en el ledger (es append-only, ADR-0008): en ese caso basta con volver
el código.

Revision ID: 0129
Revises: 0128
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None

_KINDS = "'turn','agent_run','classifier','composer','embedding','eval','generacion'"


def _kind(kinds: str) -> None:
    op.execute("ALTER TABLE usage_ledger DROP CONSTRAINT IF EXISTS ck_usage_kind")
    op.execute(f"ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_kind CHECK (kind IN ({kinds})) NOT VALID")
    op.execute("ALTER TABLE usage_ledger VALIDATE CONSTRAINT ck_usage_kind")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _kind(f"{_KINDS},'vision','stt'")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _kind(_KINDS)
