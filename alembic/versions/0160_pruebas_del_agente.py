"""Pruebas del agente por el dueño (HU-AOS-100, ADR-0212 §1, SPEC-53).

Tres tablas de NEGOCIO, con `tenant_id`, RLS forzada y borrado lógico (`deleted_at`, invariante 8):

- `agent_test_cases`: escenarios de un agente (objetivo del cliente simulado, criterios, tools esperadas y
  prohibidas). `origen` ia | manual; `estado` borrador | aprobado. Título único por agente entre los vivos
  (el límite de 50 casos por agente lo valida la aplicación).
- `agent_test_runs`: una corrida de escenarios con su `k`, la versión probada y los créditos (estimados, de la
  cuota gratis y reales brutos). El índice parcial `uq_agent_test_runs_activa` deja UNA sola corrida activa
  por negocio (ADR-0212 §4: 409 `corrida_en_curso`), también ante dos peticiones a la vez.
- `agent_test_results`: un resultado por escenario e intento, con la transcripción (la conversación de la
  corrida se purga al terminar cada escenario) y el arreglo sugerido.

Las claves foráneas son COMPUESTAS `(tenant_id, …)` (patrón de 0069): un resultado no puede apuntar a un caso, una
corrida o un agente de otro negocio, aunque la RLS ya oculte esas filas. `created_by` y `pedida_por` son uuid sin FK
(como `agents.created_by`): salen de la sesión del administrador, nunca del cuerpo de la petición.

Además `usage_ledger.kind` admite `'prueba_agente'` (juez, arreglo y turnos consolidados de una corrida, ADR-0212
§4). El CHECK se recrea `NOT VALID` y NO se valida aquí: `ADD CONSTRAINT` toma ACCESS EXCLUSIVE y `VALIDATE` en la misma
transacción lo mantendría durante todo el escaneo de una tabla de solo inserción que crece. La lista anterior es un
subconjunto, así que ninguna fila existente la viola y validar no aporta nada; las filas nuevas sí quedan controladas.

Permisos de `app_runtime`: SELECT, INSERT y UPDATE. Sin DELETE: borrar es lógico. `purge_tenant` (0072) descubre
las tablas por la columna `tenant_id` y las cubre sin cambios.

**Rollback:** primero el código anterior, después el `downgrade`. Borra las tres tablas con sus datos y falla si ya
hay filas `prueba_agente` en el ledger (es de solo inserción, ADR-0008): en ese caso basta con volver el código.

Revision ID: 0160
Revises: 0159
Create Date: 2026-11-26
"""

from __future__ import annotations

from alembic import op

revision = "0160"
down_revision = "0159"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TABLAS = ("agent_test_cases", "agent_test_runs", "agent_test_results")
_KINDS = "'turn','agent_run','classifier','composer','embedding','eval','generacion','vision','stt','tts'"


def _kind(kinds: str) -> None:
    op.execute("ALTER TABLE usage_ledger DROP CONSTRAINT IF EXISTS ck_usage_kind")
    op.execute(f"ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_kind CHECK (kind IN ({kinds})) NOT VALID")


def _tablas() -> None:
    op.execute(
        """
        CREATE TABLE agent_test_cases (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL,
          agent_id uuid NOT NULL,
          titulo text NOT NULL,
          objetivo_cliente text NOT NULL,
          datos_cliente jsonb NOT NULL DEFAULT '{}'::jsonb,
          criterios text[] NOT NULL DEFAULT '{}',
          tools_esperadas text[] NOT NULL DEFAULT '{}',
          tools_prohibidas text[] NOT NULL DEFAULT '{}',
          origen text NOT NULL,
          estado text NOT NULL DEFAULT 'borrador',
          created_by uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          deleted_at timestamptz,
          CONSTRAINT uq_agent_test_cases_tenant_id UNIQUE (tenant_id, id),
          CONSTRAINT fk_agent_test_cases_agente FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id),
          CONSTRAINT ck_agent_test_cases_titulo CHECK (length(btrim(titulo)) BETWEEN 1 AND 120),
          CONSTRAINT ck_agent_test_cases_criterios CHECK (cardinality(criterios) <= 20),
          CONSTRAINT ck_agent_test_cases_tools CHECK (NOT (tools_esperadas && tools_prohibidas)),
          CONSTRAINT ck_agent_test_cases_objetivo CHECK (length(objetivo_cliente) BETWEEN 1 AND 2000),
          CONSTRAINT ck_agent_test_cases_origen CHECK (origen IN ('ia', 'manual')),
          CONSTRAINT ck_agent_test_cases_estado CHECK (estado IN ('borrador', 'aprobado'))
        );
        CREATE UNIQUE INDEX uq_agent_test_cases_titulo
          ON agent_test_cases (tenant_id, agent_id, lower(titulo)) WHERE deleted_at IS NULL;
        CREATE INDEX ix_agent_test_cases_agente
          ON agent_test_cases (tenant_id, agent_id) WHERE deleted_at IS NULL;

        CREATE TABLE agent_test_runs (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL,
          agent_id uuid NOT NULL,
          estado text NOT NULL DEFAULT 'en_cola',
          k integer NOT NULL DEFAULT 1,
          version_probada jsonb NOT NULL DEFAULT '{}'::jsonb,
          creditos_estimados numeric(12, 1) NOT NULL DEFAULT 0,
          creditos_de_cuota numeric(12, 1) NOT NULL DEFAULT 0,
          creditos_reales numeric(12, 1) NOT NULL DEFAULT 0,
          tope_creditos numeric(12, 1) NOT NULL DEFAULT 0,
          pedida_por uuid,
          iniciada_en timestamptz,
          terminada_en timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          deleted_at timestamptz,
          CONSTRAINT uq_agent_test_runs_tenant_id UNIQUE (tenant_id, id),
          CONSTRAINT fk_agent_test_runs_agente FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id),
          CONSTRAINT ck_agent_test_runs_estado CHECK (
            estado IN ('en_cola', 'corriendo', 'terminada', 'cancelada',
                       'cortada_por_tope', 'cortada_por_saldo', 'fallo')
          ),
          -- Techo duro de seguridad; el tope editable por negocio vive en `tenants.settings.pruebas`.
          CONSTRAINT ck_agent_test_runs_k CHECK (k BETWEEN 1 AND 10),
          CONSTRAINT ck_agent_test_runs_creditos CHECK (
            creditos_estimados >= 0 AND creditos_de_cuota >= 0 AND creditos_reales >= 0 AND tope_creditos >= 0
          ),
          CONSTRAINT ck_agent_test_runs_tiempos CHECK (terminada_en IS NULL OR terminada_en >= iniciada_en),
          -- Una corrida activa no se borra: si no, liberaría el cupo de `uq_agent_test_runs_activa` sin terminar.
          CONSTRAINT ck_agent_test_runs_borrado CHECK (
            deleted_at IS NULL OR estado NOT IN ('en_cola', 'corriendo')
          )
        );
        CREATE UNIQUE INDEX uq_agent_test_runs_activa
          ON agent_test_runs (tenant_id) WHERE estado IN ('en_cola', 'corriendo');
        CREATE INDEX ix_agent_test_runs_agente
          ON agent_test_runs (tenant_id, agent_id, created_at DESC) WHERE deleted_at IS NULL;

        CREATE TABLE agent_test_results (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          tenant_id uuid NOT NULL,
          run_id uuid NOT NULL,
          case_id uuid NOT NULL,
          intento integer NOT NULL DEFAULT 1,
          estado text NOT NULL,
          fin text,
          transcripcion jsonb NOT NULL DEFAULT '[]'::jsonb,
          tools_llamadas text[] NOT NULL DEFAULT '{}',
          criterio_incumplido text,
          razon text,
          arreglo jsonb,
          arreglo_aplicado_en timestamptz,
          juez_equivocado boolean NOT NULL DEFAULT false,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          deleted_at timestamptz,
          CONSTRAINT fk_agent_test_results_corrida FOREIGN KEY (tenant_id, run_id)
            REFERENCES agent_test_runs (tenant_id, id),
          CONSTRAINT fk_agent_test_results_caso FOREIGN KEY (tenant_id, case_id)
            REFERENCES agent_test_cases (tenant_id, id),
          CONSTRAINT ck_agent_test_results_arreglo CHECK (arreglo_aplicado_en IS NULL OR arreglo IS NOT NULL),
          CONSTRAINT ck_agent_test_results_intento CHECK (intento >= 1),
          CONSTRAINT ck_agent_test_results_estado CHECK (estado IN ('pasa', 'falla', 'no_se_pudo_probar')),
          CONSTRAINT ck_agent_test_results_fin CHECK (fin IS NULL OR fin IN ('objetivo', 'se_rindio', 'tope'))
        );
        CREATE UNIQUE INDEX uq_agent_test_results_intento
          ON agent_test_results (tenant_id, run_id, case_id, intento) WHERE deleted_at IS NULL;
        CREATE INDEX ix_agent_test_results_caso ON agent_test_results (tenant_id, case_id) WHERE deleted_at IS NULL;
        """
    )


def _rls(tabla: str) -> None:
    op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {tabla}_tenant_isolation ON {tabla}")
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
                GRANT SELECT, INSERT, UPDATE ON {tabla} TO app_runtime;
            END IF;
        END $do$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _tablas()
    for tabla in _TABLAS:
        _rls(tabla)
    _kind(f"{_KINDS},'prueba_agente'")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _kind(_KINDS)
    for tabla in reversed(_TABLAS):
        op.execute(f"DROP TABLE IF EXISTS {tabla}")
