"""Agent OS · expand 3/5: tablas de negocio nuevas (HU-AOS-02, ADR-0113..0118).

Crea, vacías, las tablas del Agent OS (diseño de SPEC-26, §4.3):

- agentes: `agents`, `agent_drafts`, `agent_versions`, `agent_instances` (ADR-0113, ADR-0117);
- permisos A2A con scope de negocio: `a2a_graphs`, `a2a_graph_versions`, `agent_links` (ADR-0116 §2);
- habilidades instaladas con scope: `pack_installations`, sin versión (ADR-0118 §2);
- releases por conexión: `connection_releases`, `release_components`, `release_events` (ADR-0117);
- estado de la conversación: `conversation_tasks` (proyección) y `task_events` (ADR-0114 §1);
- ledger del Coordinador: `core_decisions`, `turn_outcomes`;
- A2A: `a2a_conversations` (proyección) y `a2a_events` (ADR-0116 §4);
- memoria y mejora: `memory_facts` (ADR-0114 §3), `trigger_embeddings`, `turn_feedback`,
  `improvement_proposals`.

Todas son tablas de NEGOCIO: `tenant_id uuid not null`, RLS habilitada y FORZADA con la policy de
aislamiento (`nullif` en USING y WITH CHECK), índices que empiezan por `tenant_id`, `UNIQUE
(tenant_id, id)` en las que son padre y FKs compuestas `(tenant_id, …)` en TODA referencia a otra
entidad del negocio: una referencia entre negocios es imposible a nivel de esquema (ADR-0116 §4).

Permisos de `app_runtime` (los default privileges de 0003 dan SELECT, INSERT, UPDATE y DELETE: se
revoca todo y se otorga lo mínimo):
- append-only (`prevent_mutation()` sin argumentos, 0067): solo SELECT e INSERT;
- el resto: SELECT, INSERT y UPDATE. Ninguna tiene DELETE (invariante 8: borrado lógico;
  `purge_tenant` es SECURITY DEFINER y desactiva los triggers).

`agent_instance_checks()`: en INSERT, el rol coincide con el `kind` del agente y la conexión es
conversacional; si el agente o la conexión no existen EN ESE NEGOCIO, no lanza y deja que la FK
compuesta lo rechace. En UPDATE, `tenant_id`, `connection_id`, `agent_id` y `role` son inmutables:
`release_components` fija instancia → versión y mover una instancia corrompería releases viejas.
Los triggers del Coordinador ("release activa ⇒ core habilitado") van en 0070, con las columnas
de `channel_connections` que miran.

`trigger_embeddings.embedding` usa `EMBEDDING_DIMENSIONS` (como `memory_vectors` desde 0046).
Ninguna VISTA nueva con `tenant_id` en `public` (`purge_tenant` las trataría como tablas).

Sin cambio de conducta: nada en runtime lee ni escribe estas tablas, y no hay modelo SQLAlchemy
hasta la HU que primero las use (HU-AOS-04, 10, 12…): un modelo sin consumidor es código muerto.
Tablas nuevas y vacías: las FKs hacia `conversations`, `customers` y `channel_connections` toman
SHARE ROW EXCLUSIVE sobre ellas por milisegundos (con `lock_timeout`).

Revision ID: 0069
Revises: 0068
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op
from core.config import get_settings

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"

# Orden de creación (padres primero). El downgrade las borra al revés.
_APPEND_ONLY = (
    "agent_versions",
    "a2a_graph_versions",
    "agent_links",
    "connection_releases",
    "release_components",
    "release_events",
    "task_events",
    "core_decisions",
    "turn_outcomes",
    "a2a_events",
    "turn_feedback",
)
TABLAS = (
    "agents",
    "agent_drafts",
    "agent_versions",
    "agent_instances",
    "a2a_graphs",
    "a2a_graph_versions",
    "agent_links",
    "pack_installations",
    "connection_releases",
    "release_components",
    "release_events",
    "conversation_tasks",
    "task_events",
    "core_decisions",
    "turn_outcomes",
    "a2a_conversations",
    "a2a_events",
    "memory_facts",
    "trigger_embeddings",
    "turn_feedback",
    "improvement_proposals",
)


def _rls(tabla: str) -> None:
    op.execute(f"ALTER TABLE {tabla} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {tabla} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {tabla}_tenant_isolation ON {tabla} "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    permisos = "SELECT, INSERT" if tabla in _APPEND_ONLY else "SELECT, INSERT, UPDATE"
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
    if tabla in _APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {tabla}_immutable BEFORE UPDATE OR DELETE ON {tabla} "
            "FOR EACH ROW EXECUTE FUNCTION prevent_mutation()"
        )


def _ddl(dim: int) -> dict[str, str]:
    return {
        # ------------------------------------------------------------- agentes
        "agents": """
            CREATE TABLE agents (
              id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id       uuid NOT NULL,
              key             text NOT NULL CHECK (key ~ '^[a-z0-9_]{2,40}$'),
              kind            text NOT NULL CHECK (kind IN ('core','specialist','backoffice')),
              name            text NOT NULL,
              description     text NOT NULL DEFAULT '',
              source_template text,
              status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
              created_by      uuid,
              created_at      timestamptz NOT NULL DEFAULT now(),
              updated_at      timestamptz NOT NULL DEFAULT now(),
              deleted_at      timestamptz,
              CONSTRAINT uq_agents_tenant_id UNIQUE (tenant_id, id)
            );
            CREATE UNIQUE INDEX uq_agents_tenant_key ON agents (tenant_id, key) WHERE deleted_at IS NULL;
        """,
        "agent_drafts": """
            CREATE TABLE agent_drafts (
              tenant_id       uuid NOT NULL,
              agent_id        uuid NOT NULL,
              content         jsonb NOT NULL,
              base_version_id uuid,
              lock_version    integer NOT NULL DEFAULT 0 CHECK (lock_version >= 0),
              updated_by      uuid,
              created_at      timestamptz NOT NULL DEFAULT now(),
              updated_at      timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (tenant_id, agent_id),
              FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id)
            );
        """,
        "agent_versions": """
            CREATE TABLE agent_versions (
              id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id            uuid NOT NULL,
              agent_id             uuid NOT NULL,
              version              integer NOT NULL CHECK (version > 0),
              label                text NOT NULL,
              contract             text NOT NULL CHECK (contract IN ('agentspec/3','kernelspec/1')),
              content              jsonb NOT NULL,
              content_hash         text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
              legacy_agent_spec_id uuid,
              notes                text NOT NULL DEFAULT '',
              created_by           uuid,
              created_at           timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_agent_versions_tenant_id UNIQUE (tenant_id, id),
              CONSTRAINT uq_agent_versions_number    UNIQUE (tenant_id, agent_id, version),
              CONSTRAINT uq_agent_versions_hash      UNIQUE (tenant_id, agent_id, content_hash),
              FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id)
            );
        """,
        "agent_instances": """
            CREATE TABLE agent_instances (
              id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id     uuid NOT NULL,
              connection_id uuid NOT NULL,
              agent_id      uuid NOT NULL,
              role          text NOT NULL CHECK (role IN ('core','specialist','backoffice')),
              is_default    boolean NOT NULL DEFAULT false,
              priority      smallint NOT NULL DEFAULT 100,
              enabled       boolean NOT NULL DEFAULT true,
              overrides     jsonb NOT NULL DEFAULT '{}'::jsonb,
              created_at    timestamptz NOT NULL DEFAULT now(),
              updated_at    timestamptz NOT NULL DEFAULT now(),
              deleted_at    timestamptz,
              CONSTRAINT uq_agent_instances_tenant_id UNIQUE (tenant_id, id),
              CONSTRAINT ck_agent_instances_default CHECK (NOT is_default OR role = 'specialist'),
              FOREIGN KEY (tenant_id, connection_id) REFERENCES channel_connections (tenant_id, id),
              FOREIGN KEY (tenant_id, agent_id)      REFERENCES agents (tenant_id, id)
            );
            CREATE UNIQUE INDEX uq_agent_instances_agent    ON agent_instances (tenant_id, connection_id, agent_id)
              WHERE deleted_at IS NULL;
            CREATE UNIQUE INDEX uq_agent_instances_one_core ON agent_instances (tenant_id, connection_id)
              WHERE role = 'core' AND deleted_at IS NULL;
            CREATE UNIQUE INDEX uq_agent_instances_default  ON agent_instances (tenant_id, connection_id)
              WHERE is_default AND deleted_at IS NULL;
            CREATE INDEX ix_agent_instances_agent ON agent_instances (tenant_id, agent_id);

            CREATE OR REPLACE FUNCTION agent_instance_checks() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
              v_kind text;
              v_conv boolean;
            BEGIN
              IF TG_OP = 'UPDATE' THEN
                IF (NEW.tenant_id, NEW.connection_id, NEW.agent_id, NEW.role)
                   IS DISTINCT FROM (OLD.tenant_id, OLD.connection_id, OLD.agent_id, OLD.role) THEN
                  RAISE EXCEPTION 'agent_instances: negocio, conexión, agente y rol no cambian (crear otra instancia)';
                END IF;
                RETURN NEW;
              END IF;
              -- Si no existe en ESTE negocio, la FK compuesta lo rechaza (ForeignKeyViolation).
              SELECT a.kind INTO v_kind FROM agents a
               WHERE a.tenant_id = NEW.tenant_id AND a.id = NEW.agent_id;
              IF FOUND AND v_kind <> NEW.role THEN
                RAISE EXCEPTION 'el rol % no coincide con el agente (%)', NEW.role, v_kind;
              END IF;
              SELECT ct.produces_conversations INTO v_conv
                FROM channel_connections cc LEFT JOIN connection_types ct ON ct.id = cc.type_id
               WHERE cc.tenant_id = NEW.tenant_id AND cc.id = NEW.connection_id;
              IF FOUND AND NOT coalesce(v_conv, false) THEN
                RAISE EXCEPTION 'la conexión % no es conversacional', NEW.connection_id;
              END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER agent_instances_checks BEFORE INSERT OR UPDATE ON agent_instances
              FOR EACH ROW EXECUTE FUNCTION agent_instance_checks();
        """,
        # ------------------------------------------------------------- grafo A2A
        "a2a_graphs": """
            CREATE TABLE a2a_graphs (
              id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id  uuid NOT NULL,
              name       text NOT NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now(),
              deleted_at timestamptz,
              CONSTRAINT uq_a2a_graphs_tenant_id UNIQUE (tenant_id, id)
            );
            CREATE UNIQUE INDEX uq_a2a_graphs_tenant ON a2a_graphs (tenant_id) WHERE deleted_at IS NULL;
        """,
        "a2a_graph_versions": """
            CREATE TABLE a2a_graph_versions (
              id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id    uuid NOT NULL,
              graph_id     uuid NOT NULL,
              version      integer NOT NULL CHECK (version > 0),
              content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
              created_by   uuid,
              created_at   timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_a2a_graph_versions_tenant_id UNIQUE (tenant_id, id),
              CONSTRAINT uq_a2a_graph_versions_number    UNIQUE (tenant_id, graph_id, version),
              FOREIGN KEY (tenant_id, graph_id) REFERENCES a2a_graphs (tenant_id, id)
            );
        """,
        "agent_links": """
            CREATE TABLE agent_links (
              id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id        uuid NOT NULL,
              graph_version_id uuid NOT NULL,
              from_agent_id    uuid NOT NULL,
              to_agent_id      uuid NOT NULL,
              modes            text[] NOT NULL,
              intents          text[] NOT NULL DEFAULT '{}',
              max_depth        smallint NOT NULL DEFAULT 3 CHECK (max_depth BETWEEN 1 AND 5),
              timeout_ms       integer  NOT NULL DEFAULT 15000 CHECK (timeout_ms BETWEEN 500 AND 60000),
              budget_usd       numeric(10,6) NOT NULL DEFAULT 0.02 CHECK (budget_usd >= 0),
              budget_tokens    integer  NOT NULL DEFAULT 20000 CHECK (budget_tokens >= 0),
              data_classes     text[]   NOT NULL DEFAULT ARRAY['public','internal'],
              created_at       timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT ck_agent_links_modes CHECK (cardinality(modes) > 0
                         AND modes <@ ARRAY['delegate','handoff','query']),
              CONSTRAINT ck_agent_links_data  CHECK (data_classes <@ ARRAY['public','internal','pii','sensitive']),
              CONSTRAINT ck_agent_links_self  CHECK (from_agent_id <> to_agent_id),
              FOREIGN KEY (tenant_id, graph_version_id) REFERENCES a2a_graph_versions (tenant_id, id),
              FOREIGN KEY (tenant_id, from_agent_id)    REFERENCES agents (tenant_id, id),
              FOREIGN KEY (tenant_id, to_agent_id)      REFERENCES agents (tenant_id, id)
            );
            CREATE UNIQUE INDEX uq_agent_links_pair
              ON agent_links (tenant_id, graph_version_id, from_agent_id, to_agent_id);
        """,
        # ------------------------------------------------------------- habilidades instaladas
        "pack_installations": """
            CREATE TABLE pack_installations (
              id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id        uuid NOT NULL,
              pack_id          text NOT NULL REFERENCES plugins(id),
              scope            text NOT NULL CHECK (scope IN ('tenant','connection')),
              connection_id    uuid,
              status           text NOT NULL DEFAULT 'pending_config'
                               CHECK (status IN ('pending_config','active','disabled','error')),
              config_encrypted text,
              installed_by     uuid,
              installed_at     timestamptz NOT NULL DEFAULT now(),
              updated_at       timestamptz NOT NULL DEFAULT now(),
              deleted_at       timestamptz,
              CONSTRAINT uq_pack_installations_tenant_id UNIQUE (tenant_id, id),
              CONSTRAINT ck_pack_installations_scope CHECK ((scope = 'tenant' AND connection_id IS NULL)
                                                         OR (scope = 'connection' AND connection_id IS NOT NULL)),
              FOREIGN KEY (tenant_id, connection_id) REFERENCES channel_connections (tenant_id, id)
            );
            CREATE UNIQUE INDEX uq_pack_installations_scope ON pack_installations
              (tenant_id, pack_id, scope, coalesce(connection_id, '00000000-0000-0000-0000-000000000000'::uuid))
              WHERE deleted_at IS NULL;
            CREATE INDEX ix_pack_installations_connection ON pack_installations (tenant_id, connection_id)
              WHERE connection_id IS NOT NULL;
        """,
        # ------------------------------------------------------------- releases
        "connection_releases": """
            CREATE TABLE connection_releases (
              id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id        uuid NOT NULL,
              connection_id    uuid NOT NULL,
              number           integer NOT NULL CHECK (number > 0),
              origin           text NOT NULL DEFAULT 'owner' CHECK (origin IN ('owner','platform')),
              graph_version_id uuid,
              manifest         jsonb NOT NULL,
              manifest_hash    text NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
              created_by       uuid,
              created_at       timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_connection_releases_tenant_id UNIQUE (tenant_id, id),
              CONSTRAINT uq_connection_releases_conn_id   UNIQUE (tenant_id, connection_id, id),
              CONSTRAINT uq_connection_releases_number    UNIQUE (tenant_id, connection_id, number),
              CONSTRAINT uq_connection_releases_hash      UNIQUE (tenant_id, connection_id, manifest_hash),
              FOREIGN KEY (tenant_id, connection_id)    REFERENCES channel_connections (tenant_id, id),
              FOREIGN KEY (tenant_id, graph_version_id) REFERENCES a2a_graph_versions (tenant_id, id)
            );
        """,
        "release_components": """
            CREATE TABLE release_components (
              tenant_id         uuid NOT NULL,
              release_id        uuid NOT NULL,
              agent_instance_id uuid NOT NULL,
              agent_version_id  uuid NOT NULL,
              created_at        timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (tenant_id, release_id, agent_instance_id),
              FOREIGN KEY (tenant_id, release_id)        REFERENCES connection_releases (tenant_id, id),
              FOREIGN KEY (tenant_id, agent_instance_id) REFERENCES agent_instances (tenant_id, id),
              FOREIGN KEY (tenant_id, agent_version_id)  REFERENCES agent_versions (tenant_id, id)
            );
        """,
        "release_events": """
            CREATE TABLE release_events (
              id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id     uuid NOT NULL,
              connection_id uuid NOT NULL,
              release_id    uuid NOT NULL,
              type          text NOT NULL CHECK (type IN ('built','evaluated','approved','rejected','promoted',
                                                          'canary_started','canary_stopped','rolled_back','retired',
                                                          'invalid')),
              data          jsonb NOT NULL DEFAULT '{}'::jsonb,
              actor         text NOT NULL,
              created_at    timestamptz NOT NULL DEFAULT now(),
              FOREIGN KEY (tenant_id, connection_id, release_id)
                REFERENCES connection_releases (tenant_id, connection_id, id)
            );
            CREATE INDEX ix_release_events_conn ON release_events (tenant_id, connection_id, created_at);
            CREATE INDEX ix_release_events_release ON release_events (tenant_id, release_id, created_at);
        """,
        # ------------------------------------------------------------- tareas
        "conversation_tasks": """
            CREATE TABLE conversation_tasks (
              id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id         uuid NOT NULL,
              conversation_id   uuid NOT NULL,
              procedure_id      text NOT NULL,
              procedure_version text NOT NULL,
              owner_instance_id uuid,
              parent_task_id    uuid,
              status            text NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','suspended','completed','cancelled','failed','escalated')),
              stage             text NOT NULL,
              slots             jsonb NOT NULL DEFAULT '{}'::jsonb,
              stack_position    smallint NOT NULL DEFAULT 0 CHECK (stack_position >= 0),
              last_seq          integer NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
              started_turn_id   uuid,
              completed_at      timestamptz,
              created_at        timestamptz NOT NULL DEFAULT now(),
              updated_at        timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_conversation_tasks_tenant_id UNIQUE (tenant_id, id),
              FOREIGN KEY (tenant_id, conversation_id)   REFERENCES conversations (tenant_id, id),
              FOREIGN KEY (tenant_id, owner_instance_id) REFERENCES agent_instances (tenant_id, id),
              FOREIGN KEY (tenant_id, parent_task_id)    REFERENCES conversation_tasks (tenant_id, id)
            );
            CREATE UNIQUE INDEX uq_conversation_tasks_focus ON conversation_tasks (tenant_id, conversation_id)
              WHERE status = 'active' AND stack_position = 0 AND parent_task_id IS NULL;
            CREATE UNIQUE INDEX uq_conversation_tasks_open_procedure
              ON conversation_tasks (tenant_id, conversation_id, procedure_id)
              WHERE status IN ('active','suspended') AND parent_task_id IS NULL;
            CREATE INDEX ix_conversation_tasks_conv ON conversation_tasks (tenant_id, conversation_id, status);
            CREATE INDEX ix_conversation_tasks_parent ON conversation_tasks (tenant_id, parent_task_id)
              WHERE parent_task_id IS NOT NULL;
        """,
        "task_events": """
            CREATE TABLE task_events (
              id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id  uuid NOT NULL,
              task_id    uuid NOT NULL,
              seq        integer NOT NULL CHECK (seq > 0),
              type       text NOT NULL CHECK (type IN ('started','slot_set','slot_cleared','stage_entered',
                                                      'precondition_failed','action_requested','action_succeeded',
                                                      'action_failed','confirmation_requested','confirmed',
                                                      'suspended','resumed','owner_changed','revalidated',
                                                      'episode_expired','completed','cancelled','failed',
                                                      'escalated')),
              data       jsonb NOT NULL DEFAULT '{}'::jsonb,
              actor      text NOT NULL CHECK (actor IN ('llm','kernel','tool','human','backoffice')),
              turn_id    uuid,
              created_at timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_task_events_seq UNIQUE (tenant_id, task_id, seq),
              FOREIGN KEY (tenant_id, task_id) REFERENCES conversation_tasks (tenant_id, id)
            );
            CREATE INDEX ix_task_events_turn ON task_events (tenant_id, turn_id) WHERE turn_id IS NOT NULL;
        """,
        # ------------------------------------------------------------- ledger del Coordinador
        "core_decisions": """
            CREATE TABLE core_decisions (
              id                  uuid PRIMARY KEY,
              tenant_id           uuid NOT NULL,
              connection_id       uuid,
              conversation_id     uuid NOT NULL,
              release_id          uuid,
              release_lane        text CHECK (release_lane IS NULL
                                              OR release_lane IN ('stable','canary','pinned','legacy')),
              inbound_message_ids uuid[] NOT NULL DEFAULT '{}',
              outcome             text NOT NULL CHECK (outcome IN ('routed','blocked','escalated','ignored')),
              blocked_by          text CHECK (blocked_by IN ('kill_switch','connection_paused','quota','budget',
                                                             'out_of_hours','escalated_conversation',
                                                             'escalation_keyword')),
              active_instance_id  uuid,
              focus_task_id       uuid,
              route_source        text CHECK (route_source IN ('task','rule','trigger','coprocessor','default')),
              rule_id             text,
              candidates          jsonb,
              confidence          numeric(4,3) CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
              llm_tokens          integer NOT NULL DEFAULT 0 CHECK (llm_tokens >= 0),
              llm_cost_usd        numeric(10,6) NOT NULL DEFAULT 0 CHECK (llm_cost_usd >= 0),
              latency_ms          integer NOT NULL CHECK (latency_ms >= 0),
              trace_id            text,
              created_at          timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_core_decisions_tenant_id UNIQUE (tenant_id, id),
              CONSTRAINT ck_core_decisions_routed CHECK (outcome = 'routed' OR route_source IS NULL),
              FOREIGN KEY (tenant_id, connection_id)             REFERENCES channel_connections (tenant_id, id),
              FOREIGN KEY (tenant_id, connection_id, release_id) REFERENCES connection_releases (tenant_id, connection_id, id),
              FOREIGN KEY (tenant_id, conversation_id)           REFERENCES conversations (tenant_id, id),
              FOREIGN KEY (tenant_id, active_instance_id)        REFERENCES agent_instances (tenant_id, id),
              FOREIGN KEY (tenant_id, focus_task_id)             REFERENCES conversation_tasks (tenant_id, id)
            );
            CREATE INDEX ix_core_decisions_conv ON core_decisions (tenant_id, conversation_id, created_at);
            CREATE INDEX ix_core_decisions_conn ON core_decisions (tenant_id, connection_id, created_at);
        """,
        "turn_outcomes": """
            CREATE TABLE turn_outcomes (
              turn_id           uuid PRIMARY KEY,
              tenant_id         uuid NOT NULL,
              status            text NOT NULL CHECK (status IN ('replied','neutral','escalated','silent','failed')),
              verifier          jsonb NOT NULL DEFAULT '[]'::jsonb,
              verifier_retries  smallint NOT NULL DEFAULT 0 CHECK (verifier_retries >= 0),
              tasks_touched     uuid[] NOT NULL DEFAULT '{}',
              final_instance_id uuid,
              tokens_in         integer NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
              tokens_out        integer NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
              cost_usd          numeric(10,6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
              latency_ms        integer NOT NULL CHECK (latency_ms >= 0),
              created_at        timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_turn_outcomes_tenant_turn UNIQUE (tenant_id, turn_id),
              FOREIGN KEY (tenant_id, turn_id)           REFERENCES core_decisions (tenant_id, id),
              FOREIGN KEY (tenant_id, final_instance_id) REFERENCES agent_instances (tenant_id, id)
            );
        """,
        # ------------------------------------------------------------- A2A
        "a2a_conversations": """
            CREATE TABLE a2a_conversations (
              id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id             uuid NOT NULL,
              connection_id         uuid NOT NULL,
              conversation_id       uuid,
              turn_id               uuid,
              trigger               text NOT NULL DEFAULT 'turn' CHECK (trigger IN ('turn','event','cron')),
              root_mode             text NOT NULL CHECK (root_mode IN ('delegate','handoff','query',
                                                                       'broadcast','collaborate')),
              initiator_instance_id uuid NOT NULL,
              max_depth             smallint NOT NULL CHECK (max_depth BETWEEN 1 AND 5),
              budget_usd            numeric(10,6) NOT NULL CHECK (budget_usd >= 0),
              budget_tokens         integer NOT NULL CHECK (budget_tokens >= 0),
              deadline_at           timestamptz NOT NULL,
              status                text NOT NULL DEFAULT 'open'
                                    CHECK (status IN ('open','completed','failed','timed_out',
                                                      'budget_exhausted','loop_blocked')),
              spent_usd             numeric(10,6) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
              spent_tokens          integer NOT NULL DEFAULT 0 CHECK (spent_tokens >= 0),
              depth_reached         smallint NOT NULL DEFAULT 0 CHECK (depth_reached BETWEEN 0 AND 5),
              calls                 integer NOT NULL DEFAULT 0 CHECK (calls >= 0),
              trace_id              text,
              started_at            timestamptz NOT NULL DEFAULT now(),
              ended_at              timestamptz,
              CONSTRAINT uq_a2a_conversations_tenant_id UNIQUE (tenant_id, id),
              CONSTRAINT ck_a2a_conversations_trigger
                CHECK (trigger <> 'turn' OR (turn_id IS NOT NULL AND conversation_id IS NOT NULL)),
              FOREIGN KEY (tenant_id, connection_id)         REFERENCES channel_connections (tenant_id, id),
              FOREIGN KEY (tenant_id, turn_id)               REFERENCES core_decisions (tenant_id, id),
              FOREIGN KEY (tenant_id, conversation_id)       REFERENCES conversations (tenant_id, id),
              FOREIGN KEY (tenant_id, initiator_instance_id) REFERENCES agent_instances (tenant_id, id)
            );
            CREATE INDEX ix_a2a_conversations_turn ON a2a_conversations (tenant_id, turn_id);
            CREATE INDEX ix_a2a_conversations_conn ON a2a_conversations (tenant_id, connection_id, started_at);
        """,
        "a2a_events": """
            CREATE TABLE a2a_events (
              id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id             uuid NOT NULL,
              a2a_conversation_id   uuid NOT NULL,
              seq                   integer NOT NULL CHECK (seq > 0),
              call_id               uuid NOT NULL,
              parent_call_id        uuid,
              type                  text NOT NULL CHECK (type IN ('requested','denied','accepted','completed',
                                                                 'failed','timed_out','cancelled',
                                                                 'handoff_applied','control_returned',
                                                                 'consolidated','loop_detected',
                                                                 'budget_exhausted')),
              mode                  text NOT NULL CHECK (mode IN ('delegate','handoff','query',
                                                                 'broadcast','collaborate')),
              depth                 smallint NOT NULL CHECK (depth BETWEEN 1 AND 5),
              from_instance_id      uuid NOT NULL,
              to_instance_id        uuid,
              from_agent_version_id uuid,
              to_agent_version_id   uuid,
              intent                text NOT NULL,
              payload_redacted      jsonb,
              payload_hash          text CHECK (payload_hash IS NULL OR payload_hash ~ '^[0-9a-f]{64}$'),
              result_redacted       jsonb,
              reason                text,
              tokens_in             integer NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
              tokens_out            integer NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
              cost_usd              numeric(10,6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
              latency_ms            integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
              trace_id              text,
              span_id               text,
              created_at            timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_a2a_events_seq UNIQUE (tenant_id, a2a_conversation_id, seq),
              FOREIGN KEY (tenant_id, a2a_conversation_id)   REFERENCES a2a_conversations (tenant_id, id),
              FOREIGN KEY (tenant_id, from_instance_id)      REFERENCES agent_instances (tenant_id, id),
              FOREIGN KEY (tenant_id, to_instance_id)        REFERENCES agent_instances (tenant_id, id),
              FOREIGN KEY (tenant_id, from_agent_version_id) REFERENCES agent_versions (tenant_id, id),
              FOREIGN KEY (tenant_id, to_agent_version_id)   REFERENCES agent_versions (tenant_id, id)
            );
            CREATE INDEX ix_a2a_events_call ON a2a_events (tenant_id, call_id);
            CREATE INDEX ix_a2a_events_time ON a2a_events (tenant_id, created_at);
        """,
        # ------------------------------------------------------------- memoria y mejora
        "memory_facts": """
            CREATE TABLE memory_facts (
              id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id              uuid NOT NULL,
              customer_id            uuid NOT NULL,
              namespace              text NOT NULL CHECK (namespace ~ '^[a-z_]{2,30}$'),
              key                    text NOT NULL CHECK (length(key) BETWEEN 1 AND 80),
              value                  jsonb NOT NULL,
              data_class             text NOT NULL DEFAULT 'internal'
                                     CHECK (data_class IN ('public','internal','pii','sensitive')),
              confidence             numeric(3,2) NOT NULL DEFAULT 1.00 CHECK (confidence BETWEEN 0 AND 1),
              source                 text NOT NULL CHECK (source IN ('tool','agent','human','import')),
              written_by_instance_id uuid,
              source_turn_id         uuid,
              valid_from             timestamptz NOT NULL DEFAULT now(),
              superseded_at          timestamptz,
              expires_at             timestamptz,
              deleted_at             timestamptz,
              created_at             timestamptz NOT NULL DEFAULT now(),
              CONSTRAINT uq_memory_facts_tenant_id UNIQUE (tenant_id, id),
              FOREIGN KEY (tenant_id, customer_id)            REFERENCES customers (tenant_id, id),
              FOREIGN KEY (tenant_id, written_by_instance_id) REFERENCES agent_instances (tenant_id, id)
            );
            CREATE UNIQUE INDEX uq_memory_facts_current ON memory_facts (tenant_id, customer_id, namespace, key)
              WHERE superseded_at IS NULL AND deleted_at IS NULL;
            CREATE INDEX ix_memory_facts_customer ON memory_facts (tenant_id, customer_id, created_at);
        """,
        "trigger_embeddings": f"""
            CREATE TABLE trigger_embeddings (
              tenant_id       uuid NOT NULL,
              release_id      uuid NOT NULL,
              procedure_id    text NOT NULL,
              exemplar_hash   text NOT NULL,
              embedding_model text NOT NULL,
              embedding       vector({dim}) NOT NULL,
              created_at      timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (tenant_id, release_id, procedure_id, exemplar_hash, embedding_model),
              FOREIGN KEY (tenant_id, release_id) REFERENCES connection_releases (tenant_id, id)
            );
        """,
        "turn_feedback": """
            CREATE TABLE turn_feedback (
              id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id       uuid NOT NULL,
              turn_id         uuid NOT NULL,
              conversation_id uuid NOT NULL,
              source          text NOT NULL CHECK (source IN ('customer','owner','human_agent','outcome',
                                                              'guardrail','eval')),
              signal          text NOT NULL,
              score           numeric(4,2),
              note            text,
              created_by      text NOT NULL,
              created_at      timestamptz NOT NULL DEFAULT now(),
              FOREIGN KEY (tenant_id, turn_id)         REFERENCES core_decisions (tenant_id, id),
              FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, id)
            );
            CREATE INDEX ix_turn_feedback_turn ON turn_feedback (tenant_id, turn_id);
            CREATE INDEX ix_turn_feedback_time ON turn_feedback (tenant_id, created_at);
        """,
        "improvement_proposals": """
            CREATE TABLE improvement_proposals (
              id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              tenant_id     uuid NOT NULL,
              scope         text NOT NULL CHECK (scope IN ('tenant','connection')),
              connection_id uuid,
              agent_id      uuid,
              kind          text NOT NULL CHECK (kind IN ('trigger_examples','procedure_texts','playbook',
                                                          'tool_description')),
              evidence      jsonb NOT NULL,
              change        jsonb NOT NULL,
              status        text NOT NULL DEFAULT 'proposed'
                            CHECK (status IN ('proposed','evaluating','approved','rejected','applied')),
              eval_run_id   uuid,
              decided_by    uuid,
              decided_at    timestamptz,
              created_at    timestamptz NOT NULL DEFAULT now(),
              updated_at    timestamptz NOT NULL DEFAULT now(),
              deleted_at    timestamptz,
              CONSTRAINT ck_improvement_scope CHECK ((scope = 'tenant' AND connection_id IS NULL)
                                                  OR (scope = 'connection' AND connection_id IS NOT NULL)),
              FOREIGN KEY (tenant_id, connection_id) REFERENCES channel_connections (tenant_id, id),
              FOREIGN KEY (tenant_id, agent_id)      REFERENCES agents (tenant_id, id)
            );
            CREATE INDEX ix_improvement_status ON improvement_proposals (tenant_id, status, created_at)
              WHERE deleted_at IS NULL;
        """,
    }


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    ddl = _ddl(get_settings().embedding_dimensions)
    assert set(ddl) == set(TABLAS), "cada tabla nueva tiene su DDL"
    for tabla in TABLAS:
        op.execute(ddl[tabla])
        _rls(tabla)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for tabla in reversed(TABLAS):
        # DROP TABLE se lleva policy, triggers, índices y FKs de la tabla.
        op.execute(f"DROP TABLE IF EXISTS {tabla}")
    op.execute("DROP FUNCTION IF EXISTS agent_instance_checks()")
