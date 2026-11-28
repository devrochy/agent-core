"""`credit_grants` admite el abono `bonificacion`: créditos de regalo por reportes aprobados (ADR-0213 §1, HU-FB-02).

ADR-0213 enmienda ADR-0209 §1: además de `acumulado` y `recarga`, un abono puede ser `bonificacion`, los
créditos que SolversHub regala al negocio cuando un master aprueba un reporte de feedback (SPEC-54).

- `ck_credit_grants_tipo` pasa a `tipo IN ('acumulado', 'recarga', 'bonificacion')`.
- `ck_credit_grants_vence_en` pasa a `(tipo IN ('recarga', 'bonificacion') AND vence_en IS NULL) OR
  (tipo = 'acumulado' AND vence_en > ciclo)`: la bonificación no vence, igual que la recarga. Se arrastra
  al cierre con su mismo `origen` (`feedback:<report_id>`) y, con la suscripción cancelada o vencida,
  sigue la misma regla que la recarga (ADR-0213 §3).
- La unicidad `(tenant_id, ciclo, tipo, origen)`, la RLS forzada, los GRANT y `prevent_mutation()` no
  cambian.

Mismo patrón que 0153 (`ck_usage_kind`): cada CHECK se recrea `NOT VALID` (no escanea al crearse) y luego
se valida con `VALIDATE CONSTRAINT`. Ojo: Alembic corre todo en una transacción, así que el ACCESS EXCLUSIVE
que toma el `DROP`/`ADD CONSTRAINT` se sostiene hasta el commit y el `VALIDATE` escanea bajo ese lock. Se
acepta porque `credit_grants` es chica (unas pocas filas por negocio y ciclo): el escaneo dura milisegundos.
`SET LOCAL lock_timeout`: si otra transacción sostiene la tabla, la migración falla en 5 s en vez de encolar
a cada turno detrás de ella. Aditiva: las listas anteriores son un subconjunto de las nuevas, así que
ninguna fila existente las viola, y el código anterior sigue funcionando con los CHECKs nuevos (despliegue
sin orden).

**Rollback:** primero el código anterior, después el `downgrade`. La reversa solo es posible mientras no
haya filas `bonificacion` (ADR-0213 §11): la tabla es solo de inserción (ADR-0008), así que el downgrade
lo comprueba primero y falla con un mensaje claro si las hay. En ese caso basta con volver el código: la
fila se conserva y el código anterior no la suma al saldo (solo cuenta `acumulado` y `recarga`).

**Numeración:** 0161, la siguiente libre de `develop` sobre `0160_pruebas_del_agente` (ADR-0213 §11). Las
tablas de reportes van en la siguiente (HU-FB-03).

Revision ID: 0161
Revises: 0160
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0161"
down_revision = "0160"
branch_labels = None
depends_on = None

_TIPO_ANTES = "tipo IN ('acumulado', 'recarga')"
_TIPO_AHORA = "tipo IN ('acumulado', 'recarga', 'bonificacion')"
_VENCE_ANTES = "(tipo = 'recarga' AND vence_en IS NULL) OR (tipo = 'acumulado' AND vence_en > ciclo)"
_VENCE_AHORA = (
    "(tipo IN ('recarga', 'bonificacion') AND vence_en IS NULL) OR (tipo = 'acumulado' AND vence_en > ciclo)"
)


def _check(nombre: str, condicion: str) -> None:
    op.execute(f"ALTER TABLE credit_grants DROP CONSTRAINT IF EXISTS {nombre}")
    op.execute(f"ALTER TABLE credit_grants ADD CONSTRAINT {nombre} CHECK ({condicion}) NOT VALID")
    op.execute(f"ALTER TABLE credit_grants VALIDATE CONSTRAINT {nombre}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _check("ck_credit_grants_tipo", _TIPO_AHORA)
    _check("ck_credit_grants_vence_en", _VENCE_AHORA)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # El VALIDATE fallaría igual, pero con un error que no dice qué hacer.
    op.execute(
        """
        DO $do$
        DECLARE
            n bigint;
        BEGIN
            SELECT count(*) INTO n FROM credit_grants WHERE tipo = 'bonificacion';
            IF n > 0 THEN
                RAISE EXCEPTION
                    'credit_grants tiene % abono(s) bonificacion: la 0161 no se puede revertir (solo inserción, '
                    'ADR-0213 §11). Basta con volver el código anterior.', n;
            END IF;
        END $do$;
        """
    )
    _check("ck_credit_grants_vence_en", _VENCE_ANTES)
    _check("ck_credit_grants_tipo", _TIPO_ANTES)
