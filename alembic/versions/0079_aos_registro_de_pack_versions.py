"""Agent OS · registro de versiones de habilidades en `pack_versions` (HU-AOS-14, ADR-0117 §1, ADR-0118 §1).

No crea tablas: agrega la única vía de escritura del catálogo de SISTEMA `pack_versions` (0068),
la función `register_pack_version()`, que usa el sync del arranque (`agent-core db sync-packs`,
`core/releases/packs.py`). Requisitos de la revisión de seguridad de HU-AOS-02:

- `SECURITY DEFINER` con `SET search_path = pg_catalog, public, pg_temp`.
- `REVOKE ALL ... FROM PUBLIC` y sin `GRANT` a `app_runtime`: el sync corre con el rol dueño
  (`MIGRATIONS_DATABASE_URL`, solo en el contenedor `api`), justo después de migrar. Se aparta a
  propósito del patrón de `upsert_plan()` (0034), que sí da `EXECUTE` a `app_runtime`.
- Solo habilidades de la plataforma: rechaza (`not_platform`) un plugin con `owner_tenant_id`.
- Recalcula el `manifest_hash` en la base: sha256 de los bytes UTF-8 del texto recibido, que es
  el JSON canónico que arma el binario (claves ordenadas, sin espacios). No depende de cómo
  Postgres serializa `jsonb`. Si no coincide con el hash declarado, `hash_invalid` y no se
  guarda. El `manifest` se guarda como `texto::jsonb`.
- `ON CONFLICT DO NOTHING`: la misma `(pack_id, version)` con el mismo hash es `exists`; con
  OTRO hash es `hash_mismatch` (el sync alerta y no la registra: una versión es inmutable).

Resultados: `inserted` | `exists` | `yanked` (ya registrada y retirada) | `hash_mismatch` |
`hash_invalid` | `invalid_manifest` | `invalid_input` | `not_platform` | `unknown_pack`. Además, un
trigger impide volver privado (`owner_tenant_id`) un plugin que ya tiene versiones. Sin cambio de conducta: con `aos_runtime = 'legacy'` nada en runtime lee
`pack_versions`; el build de releases exige que la versión de cada habilidad esté registrada.

Revision ID: 0079
Revises: 0076
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op

revision = "0079"
down_revision = "0076"
branch_labels = None
depends_on = None

_FIRMA = "register_pack_version(text, text, text, text, text)"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION register_pack_version(
            p_pack_id text, p_version text, p_contract text, p_manifest_text text, p_manifest_hash text
        ) RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public, pg_temp
        AS $$
        DECLARE
            v_owner     uuid;
            v_existe    boolean;
            v_calculado text;
            v_guardado  text;
            v_yanked    boolean;
        BEGIN
            IF p_pack_id IS NULL OR p_version IS NULL OR p_manifest_text IS NULL OR p_manifest_hash IS NULL THEN
                RETURN 'invalid_input';
            END IF;
            -- FOR SHARE: nadie cambia el dueño del plugin entre esta lectura y el INSERT.
            SELECT true, p.owner_tenant_id INTO v_existe, v_owner
              FROM public.plugins p WHERE p.id = p_pack_id FOR SHARE;
            IF v_existe IS NULL THEN
                RETURN 'unknown_pack';
            END IF;
            IF v_owner IS NOT NULL THEN
                RETURN 'not_platform';
            END IF;
            v_calculado := encode(sha256(convert_to(p_manifest_text, 'UTF8')), 'hex');
            IF p_manifest_hash IS DISTINCT FROM v_calculado THEN
                RETURN 'hash_invalid';
            END IF;
            BEGIN
                INSERT INTO public.pack_versions (pack_id, version, contract, manifest, manifest_hash)
                VALUES (p_pack_id, p_version, coalesce(p_contract, 'pack/2'), p_manifest_text::jsonb, v_calculado)
                ON CONFLICT (pack_id, version) DO NOTHING;
            EXCEPTION
                -- Un manifiesto roto (no es JSON, NUL, versión larga, plantillas que no son lista) no
                -- tumba el registro de los demás packs: se reporta.
                WHEN check_violation OR invalid_text_representation OR untranslatable_character
                     OR not_null_violation OR string_data_right_truncation THEN
                    RETURN 'invalid_manifest';
            END;
            IF FOUND THEN
                RETURN 'inserted';
            END IF;
            SELECT pv.manifest_hash, pv.yanked INTO v_guardado, v_yanked
              FROM public.pack_versions pv WHERE pv.pack_id = p_pack_id AND pv.version = p_version;
            IF v_guardado IS DISTINCT FROM v_calculado THEN
                RETURN 'hash_mismatch';
            END IF;
            IF v_yanked THEN
                RETURN 'yanked';  -- el binario trae una versión retirada por seguridad
            END IF;
            RETURN 'exists';
        END $$;
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_FIRMA} FROM PUBLIC")
    # La invariante "pack_versions solo tiene habilidades de la plataforma" también en el esquema:
    # `app_runtime` escribe `plugins` (el plugin manager) y no puede volver privado un plugin que ya
    # tiene versiones en el catálogo global.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION plugins_owner_sin_versiones() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public, pg_temp
        AS $$
        BEGIN
            IF NEW.owner_tenant_id IS NOT NULL AND NEW.owner_tenant_id IS DISTINCT FROM OLD.owner_tenant_id
               AND EXISTS (SELECT 1 FROM public.pack_versions pv WHERE pv.pack_id = OLD.id) THEN
                RAISE EXCEPTION 'plugins: % tiene versiones en pack_versions y no puede volverse privado', OLD.id;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER plugins_owner_sin_versiones BEFORE UPDATE OF owner_tenant_id ON plugins
            FOR EACH ROW EXECUTE FUNCTION plugins_owner_sin_versiones();
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON FUNCTION {_FIRMA} FROM app_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS plugins_owner_sin_versiones ON plugins")
    op.execute("DROP FUNCTION IF EXISTS plugins_owner_sin_versiones()")
    op.execute(f"DROP FUNCTION IF EXISTS {_FIRMA}")
