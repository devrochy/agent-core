## Esquema aditivo del Agent OS (expand) (2026-10-09)

- HU-AOS-02 (#363, SPEC-26, ADR-0112..0118). Cinco migraciones aditivas, sin cambio de conducta:
  nada en runtime lee ni escribe lo nuevo.
  - `0067`: `UNIQUE (tenant_id, id)` en `channel_connections`, `conversations`, `customers` y
    `meta_connections` (y en las chicas `agent_specs` y `eval_runs`), con `CREATE UNIQUE INDEX
    CONCURRENTLY` + `ADD CONSTRAINT … USING INDEX`, una tabla por transacción; y la función genérica
    `prevent_mutation()` (columnas exentas por `TG_ARGV`; rechaza también TRUNCATE).
  - `0068`: catálogo de sistema `pack_versions` (inmutable salvo `yanked`, que no se revierte; solo
    `SELECT` para `app_runtime`), vista `agent_templates` (`security_invoker`, sin `tenant_id`) y
    `plugins.owner_tenant_id` (NULL = plataforma).
  - `0069`: 21 tablas de negocio vacías (agentes, versiones e instancias; grafo A2A; instalaciones
    con scope; releases por conexión; tareas *event-sourced*; ledger del Coordinador; A2A;
    `memory_facts`; `trigger_embeddings`; feedback y propuestas), con RLS forzada, FKs compuestas
    `(tenant_id, …)`, `prevent_mutation()` en las append-only y sin `DELETE` para `app_runtime`.
    Integridad entre entidades por FK: el rol de la instancia es el `kind` del agente, un
    componente de release es de la conexión de la release y su versión es del agente, y los dueños
    e iniciadores son instancias de esa conexión. Las referencias al turno son diferidas al COMMIT.
    `pack_installations` solo guarda texto cifrado y rechaza el plugin privado de otro negocio.
    Funciones de trigger con `search_path` fijo.
  - `0070`: columnas nullables o con default en `channel_connections`, `conversations`, `messages`,
    `usage_ledger` (`kind`) y `audit_log`; CHECKs y FKs compuestas `NOT VALID` (turno, conexión,
    instancia, versión y release); triggers diferidos "release
    activa ⇒ Coordinador" en conexiones e instancias (con `FOR SHARE` y fallo cerrado); banderas
    `tenants.aos_runtime` y `tenants.aos_procedures` con el CHECK de orden. Cada tabla en su propia
    transacción, re-ejecutable.
  - `0071`: índices concurrentes (`uq_cc_webchat_key` global, también sobre las borradas; atribución por turno, conexión y
    agente, parciales) y `VALIDATE` de `ck_usage_kind`.
- `alembic/env.py`: `transaction_per_migration=True`, para que los locks de una revisión no se
  retengan hasta el final de la tanda.
- Sin modelos SQLAlchemy en el expand: llegan con la HU que primero lea o escriba cada tabla.
  Quedan fuera la memoria por scope (`memory_vectors`, HU-AOS-36), las columnas del gate en
  `eval_*` (HU-AOS-21), el contract (HU-AOS-11/15), el resolvedor de `webchat`, el sync de
  `pack_versions` (HU-AOS-14) y la federación (HU-AOS-39).
- `docs/MULTITENANT.md`: tablas de sistema y FKs compuestas. Tests de integración
  `tests/integration/test_agent_os_esquema.py` (`aislamiento`): RLS forzada y permisos por tabla,
  0 filas desde otro negocio, FKs compuestas, triggers y `purge_tenant` con datos.
