# ADR-0140 — Mi agente y su data: procedencia de registros y API de lectura por agente

- **Estado:** propuesto (HU-AOS-47, 2026-10-18; decisiones revisables por el dueño marcadas abajo)
- **Fecha:** 2026-10-18
- **Contexto:** SPEC-26, D-35 («SolversHub vende agentes»), HU-AOS-47. **Usa:** ADR-0112 (ledger del
  Coordinador), ADR-0114 (tareas y eventos), ADR-0116 (delegación), ADR-0117/HU-AOS-15 (agente en foco del
  turno), ADR-0118 §4 (metadatos de ejecución que declara el binario), ADR-0136 (permisos por sección).

## Contexto

El dueño tiene que poder abrir cada agente que paga y ver lo que hizo: conversaciones, tareas, clientes,
registros creados (pedidos, leads, citas, promesas) y consumo. Con el runtime v3 el turno ya deja casi toda
la atribución: `usage_ledger.agent_instance_id` (el especialista en foco), `conversations.active_agent_instance_id`
(HU-AOS-15/17) y `conversation_tasks.owner_instance_id` (el motor al iniciar). Faltaba saber **qué agente creó
cada registro**, que vive en tablas de packs, y una API para leer todo por agente.

## Decisión

1. **Procedencia en el core, no en los packs.** Tabla `agent_record_origins` (migración 0095): una fila por
   registro con `pack_id`, `kind`, `record_ref`, `label` (solo referencia pública: número de pedido), `tool`,
   instancia, versión, conexión, conversación y turno. Append-only (`prevent_mutation()`), RLS forzada,
   FKs compuestas con `tenant_id` (conexión, instancia de esa conexión, versión, conversación; CHECK de conexión si
   hay instancia), `UNIQUE (tenant_id, pack_id, kind, record_ref)` (el primero que lo crea gana). Sin `release_id`
   (sale del turno en el ledger) y `turn_id` sin FK (`core_decisions` depende de la bandera del ledger).
   Sin lista cerrada de `kind`: lo declara el pack (formato acotado por CHECK).
2. **El pack declara qué crea** con `MCPTool.crea_registro = RegistroCreado(kind, id_key, es_nuevo, label_key)`.
   `es_nuevo` lo escribe el pack porque cada uno distingue a su manera el registro nuevo del existente
   (`status: created|confirmed`, `ya_existe`, `duplicate`). El core solo aplica la regla genérica (sin `error`,
   sin `ok: false`, con id) y un predicado que falla cuenta como «no creado». Declarado hoy en `crear_lead`,
   `crear_pedido` (pack_commerce), `create_calendar_event` (pack_calendar) y `cob_registrar_promesa_pago` /
   `cob_registrar_promesa_saldo` (pack_cobranza). `crea_registro` no entra al manifiesto de la release
   (`core/releases/packs.py` serializa solo nombre y scopes): no exige subir la versión de los packs. Se absorberá
   en el manifiesto de acciones v2 (HU-AOS-34), no convivirá con él.
3. **Un solo punto de escritura:** `_execute_tool_safely` del loop, justo después de ejecutar la tool registrada
   y antes de `motor.tras_accion`. Cubre la ejecución directa, la del turno de confirmación
   (`requiresConfirmation`) y las acciones gobernadas por el motor. Best-effort: un fallo se loguea sin ids y
   nunca interrumpe el turno.
4. **Detrás del runtime v3.** Solo se escribe con release del turno (`aos_runtime = 'v3'`), la misma condición
   bajo la que el ledger atribuye agente: ledger y procedencia nunca se desincronizan, y con el runtime legacy el
   turno no cambia. *Revisable:* no se ata a `kernel_ledger_enabled`, que es una bandera de observabilidad;
   la procedencia es dato de producto y no se reconstruye después.
5. **API de lectura** (`core/api/routers/agent_data.py`), todo paginado con `core/api/pagination.py` y filtrable
   por `connection_id`, `desde` y `hasta` (30 días por defecto, máximo 366):
   `GET /admin/agents/overview`, `GET /admin/agents/{ref}/{conversations,customers,tasks,records,usage}`.
   La unidad es el **agente**; su data es la de todas sus instancias (también las borradas, por la historia).
   Conversaciones, clientes y consumo salen de `usage_ledger`; tareas de `owner_instance_id`; registros de
   `agent_record_origins`. Se excluyen las conversaciones del simulador (`TEST_CHANNEL`), como en la API de
   conversaciones. El Coordinador no se lista: el turno se atribuye al especialista en foco.
6. **El General absorbe lo no atribuido** (`agent_instance_id IS NULL`: turnos legacy y de antes de v3, que
   atendió el AgentSpec v2, es decir el General). Las respuestas lo marcan con `incluye_sin_atribuir`; el overview
   trae además el agregado `sin_atribuir`. `ref` es el id del agente o el literal `general`: resuelve al agente
   `general` del negocio o, si no existe (legacy sin backfill, o el dueño lo quitó), a un **General virtual** que
   solo ve lo no atribuido. Así un negocio legacy ve «General con lo que haya» sin un UUID centinela en la API.
   *Revisable.*
7. **Permisos (ADR-0136).** Sección nueva `agents` («Agentes»): owner `write`, colaborador `read` por defecto
   (ADR-0047 §3, así que los colaboradores existentes la ven, incluido el costo), denegable por negocio.
   Conversaciones y clientes exigen además `conversations` (403); sin ella, tareas y registros llegan sin
   `conversation_id`. Los registros se filtran por el permiso del pack dueño de cada fila (`core` siempre). El
   editor de agentes (`core/api/routers/agents.py`) sigue en `settings`: lo reorganiza HU-AOS-24. *Revisable.*
8. **Aislamiento.** RLS + `tenant_id` explícito en cada consulta; un agente de otro negocio (o un `ref` que no es
   id ni `general`) es 404 con el mismo mensaje, sin eco del id.
9. **Consistencia.** `usage.consistency` trae las últimas mediciones pass^k (`eval_runs` con `k > 1`) del
   **negocio** (`scope: "negocio"`): `eval_runs` no se atribuye a un agente todavía; HU-AOS-21 lo hará por release.
10. **Ruta `/overview`.** El router se incluye antes que el de `agents`, cuyo `/{agent_id}` capturaría
    `/overview` (Starlette compara el texto y valida el UUID después: 422). Un test de integración lo cubre.

## Consecuencias y límites

- Lo que se crea fuera del turno (workers, crons, back-office, panel) queda sin procedencia: no es del agente.
- Con delegación (ADR-0116) la procedencia usa el mismo agente en foco que el ledger: si un desvío existiera,
  sería consistente entre ambas vistas.
- No hay «créditos» todavía: el consumo es turnos, tokens y costo USD de `usage_ledger`. `/admin/plan` con las
  métricas `agentes` y `colaboradores` contra los topes de HU-BILL-05 queda **pendiente** (depende de esa HU).
- Índice nuevo `ix_conversation_tasks_owner (tenant_id, owner_instance_id, updated_at)`, no parcial (el General
  lee la rama NULL). La rama NULL de `usage_ledger` usa `ix_usage_ledger_tenant_created`.
