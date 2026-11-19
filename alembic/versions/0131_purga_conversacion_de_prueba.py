"""Purga física de una conversación de prueba (HU-ARCH-14, 2026-11-14).

El playground del panel no debe dejar rastro: al cerrar o reiniciar la prueba se borran la
conversación `test`, su cliente sintético y todo lo que colgó de ellos (mensajes, tareas,
ledger, decisiones, auditoría, memoria, option_sends…). Es una excepción ACOTADA al
invariante de no borrado físico, igual que `purge_tenant` (0072): solo una conversación
`channel = 'test'` del negocio que llama, validada dentro de la función.

Solo se borran tablas del CORE. Las tablas de negocio de los packs no se tocan: con el
sandbox de HU-ARCH-14 una prueba nueva no crea pedidos, citas, cobros ni suscripciones, y
los registros viejos que ya existan (decisión del dueño: no limpiarlos) se quedan con su
cliente. Si el cliente sintético ya no tiene conversaciones ni referencias de packs, se
borra; si un pack todavía lo referencia, se conserva para no dejar registros huérfanos.

`session_replication_role = replica` desactiva los triggers de FK e inmutabilidad mientras
dura la función (se restaura al salir, por la cláusula SET). El dueño de la función necesita
`GRANT SET ON PARAMETER session_replication_role`, el mismo permiso que ya usa `purge_tenant`.

Revision ID: 0131
Revises: 0130
"""

from __future__ import annotations

from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None

_FUNCION = """
CREATE OR REPLACE FUNCTION purge_test_conversation(p_tenant_id uuid, p_conversation_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
SET session_replication_role = replica
AS $$
DECLARE
    v_customer uuid;
    rec record;
    quedan int;
    tiene_pack boolean := false;
BEGIN
    SELECT customer_id INTO v_customer
    FROM public.conversations
    WHERE id = p_conversation_id
      AND tenant_id = p_tenant_id
      AND channel = 'test'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'purge_test_conversation: % no es una conversación de prueba del negocio',
            p_conversation_id USING ERRCODE = 'no_data_found';
    END IF;

    -- Dependencias indirectas: primero las hijas, después sus padres.
    DELETE FROM public.turn_outcomes
    WHERE tenant_id = p_tenant_id
      AND turn_id IN (
          SELECT id FROM public.core_decisions
          WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id
      );
    DELETE FROM public.task_events
    WHERE tenant_id = p_tenant_id
      AND task_id IN (
          SELECT id FROM public.conversation_tasks
          WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id
      );
    DELETE FROM public.a2a_events
    WHERE tenant_id = p_tenant_id
      AND a2a_conversation_id IN (
          SELECT id FROM public.a2a_conversations
          WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id
      );
    DELETE FROM public.option_activations
    WHERE tenant_id = p_tenant_id
      AND send_id IN (
          SELECT id FROM public.option_sends
          WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id
      );

    -- Tablas del CORE con conversation_id. No se tocan las de negocio de los packs.
    DELETE FROM public.messages WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.escalations WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.llm_usage WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.audit_log WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.usage_ledger WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.memory_vectors WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.agent_record_origins WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.option_sends WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.segment_send_recipients WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.pedidos_de_alta WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.turn_feedback WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.core_decisions WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.conversation_tasks WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;
    DELETE FROM public.a2a_conversations WHERE tenant_id = p_tenant_id AND conversation_id = p_conversation_id;

    DELETE FROM public.conversations
    WHERE tenant_id = p_tenant_id AND id = p_conversation_id;

    -- El cliente sintético se comparte entre pruebas. Solo se borra si ya no queda ninguna
    -- conversación suya y ningún pack lo referencia (para no dejar pedidos/citas huérfanos).
    IF v_customer IS NOT NULL THEN
        SELECT count(*) INTO quedan
        FROM public.conversations
        WHERE tenant_id = p_tenant_id AND customer_id = v_customer;

        IF quedan = 0 THEN
            FOR rec IN
                SELECT n.nspname, c.relname
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg\\_%'
                  AND c.relkind IN ('r', 'p')
                  AND NOT c.relispartition
                  AND a.attname = 'customer_id'
                  AND NOT a.attisdropped
                  AND c.relname NOT IN ('memory_vectors', 'memory_facts', 'customers', 'pedidos_de_alta')
                  AND EXISTS (
                      SELECT 1 FROM pg_catalog.pg_attribute t
                      WHERE t.attrelid = c.oid AND t.attname = 'tenant_id' AND NOT t.attisdropped
                  )
                ORDER BY n.nspname, c.relname
            LOOP
                EXECUTE format(
                    'SELECT EXISTS(SELECT 1 FROM %I.%I WHERE tenant_id = $1 AND customer_id = $2)',
                    rec.nspname, rec.relname
                ) INTO tiene_pack USING p_tenant_id, v_customer;
                IF tiene_pack THEN
                    EXIT;
                END IF;
            END LOOP;

            IF NOT tiene_pack THEN
                DELETE FROM public.memory_vectors WHERE tenant_id = p_tenant_id AND customer_id = v_customer;
                DELETE FROM public.memory_facts WHERE tenant_id = p_tenant_id AND customer_id = v_customer;
                DELETE FROM public.pedidos_de_alta WHERE tenant_id = p_tenant_id AND customer_id = v_customer;
                DELETE FROM public.customers
                WHERE tenant_id = p_tenant_id AND id = v_customer AND channel = 'test';
            END IF;
        END IF;
    END IF;
END;
$$
"""


def upgrade() -> None:
    op.execute(_FUNCION)
    # Las conversaciones de prueba creadas antes del marcador `panel` también se excluyen de
    # métricas y se purgan al cerrar: el cliente sintético del panel las identifica.
    op.execute(
        """
        UPDATE conversations c
        SET state = coalesce(c.state, '{}'::jsonb) || '{"panel": true}'::jsonb
        FROM customers cu
        WHERE c.tenant_id = cu.tenant_id
          AND c.customer_id = cu.id
          AND c.channel = 'test'
          AND cu.channel = 'test'
          AND cu.external_id = 'panel-playground'
          AND coalesce(c.state->>'panel', 'false') <> 'true'
        """
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                EXECUTE 'REVOKE ALL ON FUNCTION purge_test_conversation(uuid, uuid) FROM PUBLIC';
                EXECUTE 'GRANT EXECUTE ON FUNCTION purge_test_conversation(uuid, uuid) TO app_runtime';
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS purge_test_conversation(uuid, uuid)")
