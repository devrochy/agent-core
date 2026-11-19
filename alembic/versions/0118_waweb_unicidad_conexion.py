"""Una sola conexión `waweb` viva por negocio (ADR-0074).

El 2026-10-28 el negocio `solvershub` tenía dos `channel_connections` con el
mismo `external_id` (`waweb:<tenant>`) y dos `waweb_sessions`. El connector
actualizaba una fila y el panel leía la otra: el estado/jid no coincidían y el
QR dependía de una clave Redis que podía quedar huérfana.

Esta migración:
1. Marca como borradas las sesiones cuya conexión ya no existe o está borrada.
2. Deja una sola conexión `waweb` viva por negocio (prefiere la activa y la más
   antigua) y borra lógicamente las sesiones de las descartadas.
3. Crea un índice único parcial para que no vuelva a ocurrir.

**Rollback:** el downgrade solo elimina el índice; no revive las filas que esta
migración marcó como borradas (son duplicados/huérfanas). Antes de bajar,
exportar `channel_connections` y `waweb_sessions` si hace falta conservarlas.

Revision ID: 0118
Revises: 0117
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # 1. Sesiones cuya conexión ya no es una `waweb` viva.
    op.execute(
        """
        UPDATE waweb_sessions ws
        SET deleted_at = now()
        WHERE ws.deleted_at IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM channel_connections cc
            WHERE cc.id = ws.connection_id
              AND cc.deleted_at IS NULL
              AND cc.provider = 'waweb'
          )
        """
    )

    # 2. Conexiones `waweb` vivas duplicadas: se conserva la activa más antigua.
    op.execute(
        """
        WITH repetidas AS (
          SELECT id,
                 row_number() OVER (
                   PARTITION BY tenant_id
                   ORDER BY active DESC, created_at, id
                 ) AS posicion
          FROM channel_connections
          WHERE provider = 'waweb' AND deleted_at IS NULL
        )
        UPDATE channel_connections cc
        SET deleted_at = now(), active = false
        FROM repetidas r
        WHERE cc.id = r.id AND r.posicion > 1
        """
    )

    # 3. Las sesiones de las conexiones descartadas tampoco deben quedar vivas.
    op.execute(
        """
        UPDATE waweb_sessions ws
        SET deleted_at = now()
        WHERE ws.deleted_at IS NULL
          AND EXISTS (
            SELECT 1
            FROM channel_connections cc
            WHERE cc.id = ws.connection_id
              AND cc.deleted_at IS NOT NULL
          )
        """
    )

    # 4. Impide reincidencia incluso si dos procesos crean la conexión a la vez.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_connections_waweb_viva
        ON channel_connections (tenant_id)
        WHERE provider = 'waweb' AND deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_channel_connections_waweb_viva")
