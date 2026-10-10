"""Agent OS · expand 2/5: catálogo `pack_versions` y vista `agent_templates` (HU-AOS-02, ADR-0118 §1).

Tabla de SISTEMA (sin RLS, sin `tenant_id`): `pack_versions` es el catálogo global del contenido
de cada versión de una habilidad de la plataforma (`packages/pack-*`): procedimientos, consultas,
metadatos de acciones, plantillas de agente, playbooks, schemas y escenarios. Es una excepción
documentada a la invariante 1, como `plans` (ADR-0045) y `connection_types` (ADR-0069); la lista
`docs/MULTITENANT.md`. Los plugins del negocio (ADR-0089) NO entran: el catálogo lo ve cualquier
negocio.

- `app_runtime` solo tiene SELECT (tabla y vista). Los default privileges de 0003 le dan también
  INSERT, UPDATE y DELETE: se revocan explícitamente. El sync del arranque (HU-AOS-14) insertará con
  una función SECURITY DEFINER propia.
- Inmutable salvo `yanked` (retirada por seguridad): trigger con `prevent_mutation('yanked')`
  (función de 0067). La retirada es irreversible (`true` → `false` se rechaza).
- `manifest.provides.agent_templates`, si está, es un array (si no, la vista fallaría entera).
- `agent_templates` no tiene columna `tenant_id`: `purge_tenant` (0011) recorre
  `information_schema.columns` y trataría como tabla a una vista que la tuviera.

Sin cambio de conducta: nada en runtime lee ni escribe estos objetos todavía. Sin modelo
SQLAlchemy hasta la HU que los lea (HU-AOS-14): un modelo sin consumidor es código muerto.

Revision ID: 0068
Revises: 0067
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pack_versions (
            pack_id       text NOT NULL REFERENCES plugins(id),
            version       text NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
            contract      text NOT NULL DEFAULT 'pack/2',
            manifest      jsonb NOT NULL,
            manifest_hash text NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
            published_at  timestamptz NOT NULL DEFAULT now(),
            yanked        boolean NOT NULL DEFAULT false,
            PRIMARY KEY (pack_id, version),
            -- Un manifest mal formado haría fallar la vista agent_templates para TODAS las filas.
            CONSTRAINT ck_pack_versions_templates CHECK (
                jsonb_typeof(coalesce(manifest->'provides'->'agent_templates', '[]'::jsonb)) = 'array')
        );
        CREATE TRIGGER pack_versions_immutable BEFORE UPDATE OR DELETE ON pack_versions
            FOR EACH ROW EXECUTE FUNCTION prevent_mutation('yanked');

        -- La retirada por seguridad no se deshace: una versión yanked no vuelve a ser instalable.
        CREATE OR REPLACE FUNCTION pack_versions_yank_irreversible() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.yanked AND NOT NEW.yanked THEN
                RAISE EXCEPTION 'pack_versions: % % está retirada (yanked) y no se restaura', OLD.pack_id, OLD.version;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER pack_versions_yank_irreversible BEFORE UPDATE OF yanked ON pack_versions
            FOR EACH ROW EXECUTE FUNCTION pack_versions_yank_irreversible();

        CREATE VIEW agent_templates AS
        SELECT pv.pack_id, pv.version, t->>'key' AS template_key, t->>'name' AS name,
               t->>'description' AS description, t AS template
          FROM pack_versions pv,
               jsonb_array_elements(coalesce(pv.manifest->'provides'->'agent_templates', '[]'::jsonb)) t
         WHERE NOT pv.yanked;

        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON pack_versions, agent_templates FROM app_runtime;
                GRANT SELECT ON pack_versions, agent_templates TO app_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS agent_templates")
    op.execute("DROP TABLE IF EXISTS pack_versions")
    op.execute("DROP FUNCTION IF EXISTS pack_versions_yank_irreversible()")
