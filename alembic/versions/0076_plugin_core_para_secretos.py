"""Agent OS: fila `core` en el catálogo `plugins` para la instalación de secretos (HU-AOS-12).

Los secretos de las `declarativeTools` viven cifrados en una instalación `core` por scope
(`pack_installations.config_encrypted`, ADR-0118 §5 y ADR-0113 §2). `pack_installations.pack_id`
referencia `plugins(id)`, y "core" no es un módulo de entry_points, así que el plugin manager
nunca la sincroniza: la fila se siembra aquí.

- `plugins` es catálogo de SISTEMA (sin `tenant_id` ni RLS, ADR-0012): no es tabla de negocio.
- `status = 'detected'`: fuera de `('configured','active')`, así `activate_all_configured` no la
  intenta activar ni registra `plugin_in_db_not_installed` en cada arranque.
- Idempotente (`ON CONFLICT DO NOTHING`). Un INSERT de una fila en una tabla chica: sin locks
  largos.
- El downgrade solo la borra si nada la referencia: con secretos guardados, la fila se queda.
  Las tablas referentes tienen RLS forzada: el rol de migraciones debe saltarla (BYPASSRLS o
  superusuario, como en CI); si no, el DELETE choca con la FK y el downgrade falla entero
  (es transaccional), sin dejar nada a medias.

Revision ID: 0076
Revises: 0074
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op

revision = "0076"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO plugins (id, name, version, status, installed_at)
        VALUES ('core', 'Core (secretos de tools declarativas)', '0.0.0', 'detected', now())
        ON CONFLICT (id) DO UPDATE SET status = 'detected'
          WHERE plugins.status IN ('configured', 'active')
        """
    )
    # Si ya existía como plugin privado de un negocio, el trigger de 0069 dejaría la
    # instalación de secretos solo a ese negocio: se aborta.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM plugins WHERE id = 'core' AND owner_tenant_id IS NOT NULL) THEN
            RAISE EXCEPTION 'plugins.core tiene owner_tenant_id: debe ser de plataforma';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM plugins p
         WHERE p.id = 'core'
           AND NOT EXISTS (SELECT 1 FROM pack_installations pi WHERE pi.pack_id = 'core')
           AND NOT EXISTS (SELECT 1 FROM plugin_configs pc WHERE pc.plugin_id = 'core')
           AND NOT EXISTS (SELECT 1 FROM plugin_tenant_state ps WHERE ps.plugin_id = 'core')
           AND NOT EXISTS (SELECT 1 FROM pack_versions pv WHERE pv.pack_id = 'core')
        """
    )
