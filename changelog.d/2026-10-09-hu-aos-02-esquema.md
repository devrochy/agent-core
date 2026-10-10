## Esquema aditivo del Agent OS (expand) (2026-10-09)

- HU-AOS-02 (#363, SPEC-26, ADR-0112..0118). Cinco migraciones aditivas, sin cambio de conducta:
  nada en runtime lee ni escribe lo nuevo.
  - `0067`: `UNIQUE (tenant_id, id)` en `channel_connections`, `conversations`, `customers` y
    `meta_connections` (`CREATE UNIQUE INDEX CONCURRENTLY` + `ADD CONSTRAINT … USING INDEX`) y la
    función genérica `prevent_mutation()` (columnas exentas por `TG_ARGV`).
  - `0068`: catálogo de sistema `pack_versions` (inmutable salvo `yanked`, solo `SELECT` para
    `app_runtime`) y vista `agent_templates`, sin `tenant_id`.
  - `0069`: 21 tablas de negocio vacías (agentes, versiones e instancias; grafo A2A; instalaciones
    con scope; releases por conexión; tareas *event-sourced*; ledger del Coordinador; A2A;
    `memory_facts`; `trigger_embeddings`; feedback y propuestas), con RLS forzada, FKs compuestas
    `(tenant_id, …)`, `prevent_mutation()` en las append-only y sin `DELETE` para `app_runtime`.
    `agent_instance_checks()` valida rol y conexión conversacional e impide mover una instancia.
  - `0070`: columnas nullables o con default en `channel_connections`, `conversations`, `messages`,
    `usage_ledger` (`kind`) y `audit_log`; CHECKs y FKs `NOT VALID`; triggers diferidos "release
    activa ⇒ Coordinador" en conexiones e instancias; banderas `tenants.aos_runtime` y
    `tenants.aos_procedures` con el CHECK de orden.
  - `0071`: índices concurrentes (`uq_cc_webchat_key` global, atribución por turno, conexión y
    agente) y `VALIDATE` de `ck_usage_kind`.
- `alembic/env.py`: `transaction_per_migration=True`, para que los locks de una revisión no se
  retengan hasta el final de la tanda.
- Sin modelos SQLAlchemy en el expand: llegan con la HU que primero lea o escriba cada tabla.
  Quedan fuera la memoria por scope (`memory_vectors`, HU-AOS-36), las columnas del gate en
  `eval_*` (HU-AOS-21), el contract (HU-AOS-11/15), el resolvedor de `webchat` y la federación.
- `docs/MULTITENANT.md`: tablas de sistema y FKs compuestas. Tests de integración
  `tests/integration/test_agent_os_esquema.py` (`aislamiento`): RLS forzada y permisos por tabla,
  0 filas desde otro negocio, FKs compuestas, triggers y `purge_tenant` con datos.
