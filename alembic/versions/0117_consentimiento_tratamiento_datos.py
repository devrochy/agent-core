"""Consentimiento para el tratamiento de datos del cliente (HU-CRM-16, ADR-0179 decisión 8).

`customer_consents` (0100, solo inserción, RLS y GRANT ya existen) ahora también guarda:

1. `proposito = 'tratamiento_datos'`: el cliente aceptó o rechazó que el asistente guarde datos que
   el trámite no necesita.
2. `estado = 'informado'`: marcador de que el aviso ya se le mostró en esa versión (no es un
   consentimiento: `vigente` lo ignora). Solo para `tratamiento_datos`.
3. `version_aviso`: huella corta del texto del aviso; obligatoria en `tratamiento_datos`. El texto
   completo queda en `audit_log` cuando el negocio lo publica.

Solo ALTER; las restricciones nuevas se crean `NOT VALID` y se validan aparte (no bloquean la tabla).

**Rollback:** el código anterior, después el downgrade. El downgrade BORRA las filas de `tratamiento_datos`
(y los `informado`), porque el CHECK anterior no puede representarlas: son evidencia de consentimiento,
así que antes de bajar exportarlas con `pg_dump -t customer_consents`.

Revision ID: 0117
Revises: 0116
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE customer_consents ADD COLUMN IF NOT EXISTS version_aviso text")
    op.execute("ALTER TABLE customer_consents DROP CONSTRAINT IF EXISTS ck_customer_consents_estado")
    op.execute(
        "ALTER TABLE customer_consents ADD CONSTRAINT ck_customer_consents_estado "
        "CHECK (estado IN ('otorgado', 'revocado', 'informado')) NOT VALID"
    )
    op.execute("ALTER TABLE customer_consents DROP CONSTRAINT IF EXISTS ck_customer_consents_proposito")
    op.execute(
        "ALTER TABLE customer_consents ADD CONSTRAINT ck_customer_consents_proposito "
        "CHECK (proposito IN ('promociones', 'tratamiento_datos')) NOT VALID"
    )
    op.execute(
        "ALTER TABLE customer_consents ADD CONSTRAINT ck_customer_consents_version_aviso "
        "CHECK (proposito <> 'tratamiento_datos' OR (version_aviso IS NOT NULL AND char_length(version_aviso) <= 64)) NOT VALID"
    )
    op.execute(
        "ALTER TABLE customer_consents ADD CONSTRAINT ck_customer_consents_informado "
        "CHECK (estado <> 'informado' OR proposito = 'tratamiento_datos') NOT VALID"
    )
    for restriccion in ("estado", "proposito", "version_aviso", "informado"):
        op.execute(f"ALTER TABLE customer_consents VALIDATE CONSTRAINT ck_customer_consents_{restriccion}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Los estados nuevos no caben en el CHECK anterior: se quitan las filas de tratamiento_datos (el
    # downgrade de 0100 descarta la tabla entera, así que esto es lo menos destructivo que cabe). El aviso
    # y las respuestas solo existen mientras el código nuevo corre; antes de bajar, `pg_dump -t customer_consents`.
    op.execute("DELETE FROM customer_consents WHERE proposito = 'tratamiento_datos' OR estado = 'informado'")
    for restriccion in ("informado", "version_aviso", "proposito", "estado"):
        op.execute(f"ALTER TABLE customer_consents DROP CONSTRAINT IF EXISTS ck_customer_consents_{restriccion}")
    op.execute(
        "ALTER TABLE customer_consents ADD CONSTRAINT ck_customer_consents_proposito CHECK (proposito IN ('promociones'))"
    )
    op.execute(
        "ALTER TABLE customer_consents ADD CONSTRAINT ck_customer_consents_estado CHECK (estado IN ('otorgado', 'revocado'))"
    )
    op.execute("ALTER TABLE customer_consents DROP COLUMN IF EXISTS version_aviso")
