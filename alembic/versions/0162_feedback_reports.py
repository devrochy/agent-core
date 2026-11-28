"""Reportes de clientes con evidencia (HU-FB-03, ADR-0213 §5 y §6, SPEC-54).

Dos tablas de NEGOCIO, con `tenant_id`, RLS habilitada y FORZADA con la policy `nullif` (patrón de 0155) e índices
que empiezan por `tenant_id`:

- `feedback_reports`: un reporte de error, mejora o idea de cualquier usuario del negocio. `estado` recibido →
  en_revision → aprobado | rechazado (se puede decidir directo desde recibido). Los CHECK nombrados atan la decisión
  al estado: aprobado exige créditos > 0, revisor y fecha; rechazado exige `motivo_codigo` y no lleva créditos; el
  motivo `duplicado` exige `duplicado_de` distinto del propio `id`. `duplicado_de` es uuid **sin FK**: puede apuntar a
  un reporte de otro negocio, y un `ON DELETE SET NULL` sería un UPDATE sobre un reporte decidido. `area` la valida
  la aplicación (`core/feedback/catalogo.py`): el core no nombra secciones de los packs en el esquema.
- `feedback_attachments`: un adjunto por fila (clave del controlador de almacenamiento, tipo, bytes, sha256), único
  por `(tenant_id, report_id, sha256)`. Solo se crean en el mismo envío que el reporte. FK compuesta
  `(tenant_id, report_id)` (patrón de 0069): un adjunto no puede colgar de un reporte de otro negocio.

Sin borrado (invariante 8, sin `deleted_at`): `prevent_mutation()` (0067) en DELETE y TRUNCATE de las dos tablas y en
UPDATE de los adjuntos; el trigger `feedback_reports_decidido_inmutable` rechaza cualquier UPDATE de un reporte ya
aprobado o rechazado. `purge_tenant` (0072) las cubre sin cambios: descubre las tablas por la columna `tenant_id` y
apaga los triggers con `session_replication_role`; la carpeta `<slug>/feedback/` cae con la del negocio.

Bandeja entre negocios (ADR-0012, patrón de 0033): dos funciones `SECURITY DEFINER` de solo lectura que devuelven
**solo** ids y tenants; el contenido se lee después bajo `tenant_scope`. `REVOKE` a PUBLIC y `GRANT EXECUTE` solo a
`app_runtime`. Las migraciones corren con un dueño `BYPASSRLS` (0148), así que la RLS forzada no las afecta. Como
`app_runtime` es el rol de TODAS las peticiones, el «solo master» lo pone la aplicación (`actua_como_master` en el
router de HU-FB-04), igual que en 0033.

Permisos de `app_runtime`: reportes SELECT, INSERT y UPDATE solo de las columnas de la revisión (HU-FB-04): el autor,
el negocio, la clave y la fecha no se reescriben; adjuntos SELECT e INSERT.

Aditiva: tablas nuevas y vacías, sin locks sobre tablas existentes. **Rollback:** primero el código anterior (o la
bandera `FEEDBACK_ENABLED` apagada), después el `downgrade`, que borra las tablas con sus reportes; los archivos de
`<slug>/feedback/` quedan en disco y se borran a mano.

**Numeración:** 0162, después de `0161_credit_grants_bonificacion` (HU-FB-02, ADR-0213 §11).

Revision ID: 0162
Revises: 0161
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0162"
down_revision = "0161"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TABLAS = ("feedback_reports", "feedback_attachments")
_BANDEJA = "feedback_bandeja(text, text, uuid, timestamptz, uuid, integer)"
_TENANT_DE = "feedback_reporte_tenant(uuid)"


def _tablas() -> None:
    op.execute(
        """
        CREATE TABLE feedback_reports (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL,
          autor_admin_id uuid NOT NULL,
          tipo text NOT NULL,
          area text NOT NULL,
          titulo text NOT NULL,
          descripcion text NOT NULL,
          estado text NOT NULL DEFAULT 'recibido',
          revisado_por uuid,
          tomado_en timestamptz,
          decidido_en timestamptz,
          motivo_codigo text,
          motivo text,
          nota_interna text,
          creditos_otorgados integer,
          duplicado_de uuid,
          idempotency_key text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_feedback_reports_tenant_id UNIQUE (tenant_id, id),
          CONSTRAINT ck_feedback_reports_tipo CHECK (tipo IN ('error', 'mejora', 'idea')),
          CONSTRAINT ck_feedback_reports_area CHECK (length(area) BETWEEN 1 AND 80),
          CONSTRAINT ck_feedback_reports_titulo CHECK (length(btrim(titulo)) BETWEEN 3 AND 120),
          CONSTRAINT ck_feedback_reports_descripcion CHECK (length(btrim(descripcion)) BETWEEN 10 AND 2000),
          CONSTRAINT ck_feedback_reports_estado CHECK (estado IN ('recibido', 'en_revision', 'aprobado', 'rechazado')),
          CONSTRAINT ck_feedback_reports_motivo_codigo CHECK (
            motivo_codigo IS NULL OR motivo_codigo IN
              ('duplicado', 'no_reproducible', 'fuera_de_alcance', 'informacion_insuficiente', 'otro')
          ),
          CONSTRAINT ck_feedback_reports_motivo CHECK (motivo IS NULL OR length(motivo) <= 1000),
          CONSTRAINT ck_feedback_reports_nota_interna CHECK (nota_interna IS NULL OR length(nota_interna) <= 2000),
          CONSTRAINT ck_feedback_reports_idempotency_key CHECK (length(idempotency_key) BETWEEN 8 AND 128),
          CONSTRAINT ck_feedback_reports_aprobado CHECK (
            estado <> 'aprobado'
            -- `IS NOT NULL` explícito: con NULL la comparación da NULL y el CHECK la dejaría pasar.
            OR (creditos_otorgados IS NOT NULL AND creditos_otorgados > 0 AND revisado_por IS NOT NULL
                AND decidido_en IS NOT NULL
                AND motivo_codigo IS NULL)
          ),
          CONSTRAINT ck_feedback_reports_rechazado CHECK (
            estado <> 'rechazado'
            OR (motivo_codigo IS NOT NULL AND creditos_otorgados IS NULL AND revisado_por IS NOT NULL
                AND decidido_en IS NOT NULL)
          ),
          CONSTRAINT ck_feedback_reports_pendiente CHECK (
            estado NOT IN ('recibido', 'en_revision')
            OR (decidido_en IS NULL AND creditos_otorgados IS NULL AND motivo_codigo IS NULL)
          ),
          CONSTRAINT ck_feedback_reports_en_revision CHECK (
            estado <> 'en_revision' OR (revisado_por IS NOT NULL AND tomado_en IS NOT NULL)
          ),
          CONSTRAINT ck_feedback_reports_duplicado CHECK (
            (motivo_codigo IS DISTINCT FROM 'duplicado' AND duplicado_de IS NULL)
            OR (motivo_codigo = 'duplicado' AND duplicado_de IS NOT NULL AND duplicado_de <> id)
          )
        );
        CREATE INDEX ix_feedback_reports_tenant_creado ON feedback_reports (tenant_id, created_at);
        CREATE INDEX ix_feedback_reports_tenant_autor ON feedback_reports (tenant_id, autor_admin_id, created_at);
        CREATE UNIQUE INDEX uq_feedback_reports_idempotencia ON feedback_reports (tenant_id, idempotency_key);
        CREATE INDEX ix_feedback_reports_tenant_decision ON feedback_reports (tenant_id, estado, decidido_en);
        -- La bandeja del master recorre todos los negocios por estado (función definer, sin tenant).
        CREATE INDEX ix_feedback_reports_bandeja ON feedback_reports (estado, created_at, id);

        CREATE TABLE feedback_attachments (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL,
          report_id uuid NOT NULL,
          orden smallint NOT NULL,
          storage_key text NOT NULL,
          mimetype text NOT NULL,
          bytes integer NOT NULL,
          sha256 text NOT NULL,
          nombre_original text,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT fk_feedback_attachments_reporte FOREIGN KEY (tenant_id, report_id)
            REFERENCES feedback_reports (tenant_id, id),
          CONSTRAINT ck_feedback_attachments_orden CHECK (orden BETWEEN 0 AND 4),
          CONSTRAINT ck_feedback_attachments_bytes CHECK (bytes > 0),
          CONSTRAINT ck_feedback_attachments_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT ck_feedback_attachments_storage_key CHECK (length(storage_key) BETWEEN 1 AND 300),
          CONSTRAINT ck_feedback_attachments_nombre CHECK (nombre_original IS NULL OR length(nombre_original) <= 255)
        );
        CREATE UNIQUE INDEX uq_feedback_attachments_sha256 ON feedback_attachments (tenant_id, report_id, sha256);
        CREATE UNIQUE INDEX uq_feedback_attachments_orden ON feedback_attachments (tenant_id, report_id, orden);
        """
    )


def _inmutabilidad() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION feedback_reports_decidido_inmutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.estado IN ('aprobado', 'rechazado') THEN
                RAISE EXCEPTION 'feedback_reports: el reporte % ya está %, no se puede modificar', OLD.id, OLD.estado
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER feedback_reports_decidido_inmutable BEFORE UPDATE ON feedback_reports
          FOR EACH ROW EXECUTE FUNCTION feedback_reports_decidido_inmutable();
        CREATE TRIGGER feedback_reports_sin_borrar BEFORE DELETE ON feedback_reports
          FOR EACH ROW EXECUTE FUNCTION prevent_mutation();
        CREATE TRIGGER feedback_reports_sin_vaciar BEFORE TRUNCATE ON feedback_reports
          FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation();
        CREATE TRIGGER feedback_attachments_inmutable BEFORE UPDATE OR DELETE ON feedback_attachments
          FOR EACH ROW EXECUTE FUNCTION prevent_mutation();
        CREATE TRIGGER feedback_attachments_sin_vaciar BEFORE TRUNCATE ON feedback_attachments
          FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation();
        """
    )


def _rls(tabla: str, permisos: str) -> None:
    op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {tabla}_tenant_isolation ON {tabla} "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON {tabla} FROM app_runtime;
                GRANT {permisos} ON {tabla} TO app_runtime;
            END IF;
        END $do$;
        """
    )


def _funciones() -> None:
    op.execute(
        f"""
        -- Bandeja del master (HU-FB-04): SOLO ids, tenant, fecha, estado y tipo, paginada por (created_at, id)
        -- descendente. Título, descripción, autor y adjuntos se leen bajo `tenant_scope` del negocio de cada fila.
        CREATE OR REPLACE FUNCTION feedback_bandeja(
            p_estado text, p_tipo text, p_tenant uuid, p_cursor_ts timestamptz, p_cursor_id uuid, p_limit integer
        )
        RETURNS TABLE(id uuid, tenant_id uuid, created_at timestamptz, estado text, tipo text)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT r.id, r.tenant_id, r.created_at, r.estado, r.tipo
            FROM public.feedback_reports r
            WHERE (p_estado IS NULL OR r.estado = p_estado)
              AND (p_tipo IS NULL OR r.tipo = p_tipo)
              AND (p_tenant IS NULL OR r.tenant_id = p_tenant)
              AND (p_cursor_ts IS NULL OR (r.created_at, r.id) < (p_cursor_ts, p_cursor_id))
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT least(greatest(coalesce(p_limit, 50), 1), 100);
        $$;

        -- Negocio de un reporte, o null si no existe: el detalle de la bandeja y la validación de `duplicado_de`.
        CREATE OR REPLACE FUNCTION feedback_reporte_tenant(p_id uuid)
        RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT r.tenant_id FROM public.feedback_reports r WHERE r.id = p_id;
        $$;

        REVOKE ALL ON FUNCTION {_BANDEJA} FROM PUBLIC;
        REVOKE ALL ON FUNCTION {_TENANT_DE} FROM PUBLIC;
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION {_BANDEJA} TO app_runtime;
                GRANT EXECUTE ON FUNCTION {_TENANT_DE} TO app_runtime;
            END IF;
        END $do$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _tablas()
    _inmutabilidad()
    _rls(
        "feedback_reports",
        "SELECT, INSERT, UPDATE (estado, revisado_por, tomado_en, decidido_en, motivo_codigo, motivo, nota_interna, "
        "creditos_otorgados, duplicado_de, updated_at)",
    )
    _rls("feedback_attachments", "SELECT, INSERT")
    _funciones()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP FUNCTION IF EXISTS {_TENANT_DE}")
    op.execute(f"DROP FUNCTION IF EXISTS {_BANDEJA}")
    for tabla in reversed(_TABLAS):
        op.execute(f"DROP TABLE IF EXISTS {tabla}")
    op.execute("DROP FUNCTION IF EXISTS feedback_reports_decidido_inmutable()")
