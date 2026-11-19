# ADR-0114 — Estado de la conversación como tareas *event-sourced* y memoria por scope

- **Estado:** aceptado (2026-10-09, HU-AOS-01)
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-04 y HU-AOS-36 (SPEC-26). Enmienda ADR-0053 (memoria, retención y PII). Usa ADR-0091 (fecha y
  hora al final del bloque dinámico) y ADR-0117 (episodios).

## Contexto

- El estado de una conversación vive en un JSONB libre (`conversations.state`: contadores operativos como `budget_exhausted` y `turnos_sin_resolver`, `notes` y `pending_confirmation`) y en el historial que el modelo relee en cada turno.
- La memoria tiene tres capas: L1 en Redis 24 h, L2 en `customers.attributes` y L3 en `memory_vectors` con redacción de PII.
- No hay una fuente única que diga qué está en curso ni qué datos ya dio el cliente.
- Con varios agentes, esa ausencia es la primera causa de incoherencia.

## Decisión

El estado de la conversación son tareas *event-sourced* y la memoria se separa por scope, con procedencia.

1. **Tareas *event-sourced*:**
   - `task_events` es la fuente de verdad, append-only mediante un trigger `BEFORE UPDATE OR DELETE` (función genérica nueva `prevent_mutation()`, en la migración de expand de HU-AOS-02, que rechaza DELETE y todo UPDATE salvo las columnas que recibe como argumento del trigger, `TG_ARGV`). Como `usage_ledger` y `audit_log`, no lleva `deleted_at`, y `purge_tenant` la cubre al desactivar triggers (migración 0011). El `data` de cada evento lleva la versión de esquema del procedimiento, para que la reconstrucción funcione entre versiones.
   - `conversation_tasks` es la proyección (una tabla, no una vista, por `purge_tenant`), actualizada en la misma transacción y reconstruible.
   - Una tarea es una instancia de un procedimiento: etapa, datos validados, dueño, pila (una en foco y hasta `maxStack` en pausa).
   - **Tareas de un episodio anterior.** En producción la conversación no se cierra, así que una tarea abierta podría vivir para siempre. Una tarea abierta que no tuvo actividad durante todo el episodio anterior (`episode.idleMinutes`, ADR-0117) se cierra al empezar el episodio nuevo como `cancelled` con el motivo `episode_expired` (evento propio). Sus datos válidos pasan a `memory_facts` con procedencia.
   - `conversations.state` conserva solo los contadores operativos y las notas. La confirmación pendiente vive en la tarea (`confirmation_requested`/`confirmed`); `pending_confirmation` queda solo para acciones fuera de procedimiento.
2. **Tarjeta de estado:**
   - Se inyecta en el bloque dinámico del prompt, justo antes de la fecha y hora, que sigue siendo la última parte (ADR-0091), sin romper la caché del prefijo.
   - Los rótulos e instrucciones de la tarjeta viven en un playbook del core (invariante 3); el código solo serializa. Los valores de los datos son dato del cliente (invariante 7): van delimitados y escapados, y nunca se interpolan como texto libre del prompt.
   - Muestra la tarea en foco, los datos con su origen, lo que falta, lo pendiente de confirmar y las acciones de la etapa.
   - El panel "En curso" muestra lo mismo a las personas.
3. **Memoria por scope:**
   - tareas (estado estructurado), sesión, privada de un agente;
   - hechos del cliente con procedencia (`memory_facts`, escritura solo en los *namespaces* concedidos al agente). `memory_facts` es la fuente; `customers.attributes` pasa a ser su proyección para los lectores existentes (p. ej. la búsqueda de clientes de pack-calendar) y se actualiza en la misma transacción. Nadie escribe `attributes` directamente;
   - episódica, semántica de la conexión y semántica del negocio.
   - **El estado gana** a cualquier recuerdo que lo contradiga, solo para las tareas del episodio en curso.
4. **Olvido:**
   - `purge_memory` (ADR-0053) se extiende a `memory_facts`, a las claves que proyecta en `customers.attributes`, a la memoria de trabajo de Redis de **todas** las conversaciones del cliente (hoy solo la limpia con `conversation_id`) y, cuando exista caché de resultados de acciones (ADR-0117 §7), sus claves del cliente. Todo es borrado lógico (invariante 8), salvo Redis, que expira.
   - El cron de retención (`MEMORY_RETENTION_DAYS`, ADR-0053 §3) cubre `memory_facts`. Los hechos con `data_class` `pii` o `sensitive` no se embeben en `memory_vectors`, porque ahí se redactan (ADR-0053).
   - Las tareas siguen la regla de `messages` (ADR-0053): son registro del negocio, la purga por cliente no las toca y solo `purge_tenant` las borra. `task_events` no tiene excepciones a su inmutabilidad.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Seguir con `conversations.state` en JSONB | Sin validación, sin historia, sin aserciones en evals |
| Solo el historial de mensajes | El modelo reconstruye el estado en cada turno y se equivoca |
| Eventos sin proyección | Cada lectura reconstruye; la latencia del turno sube |
| Redactar los valores de `task_events` en la purga por cliente | Exige mutar una tabla append-only (deshabilitar su trigger bloquea a todos los negocios) y el dato sigue en `conversation_tasks.slots`, `messages` y `audit_log` |

## Consecuencias

**Positivas:** un turno se puede reproducir y auditar; las evals afirman sobre el estado real; el relevo humano es inmediato; los datos no se piden dos veces.

**Negativas / lo que aceptamos pagar:** más tablas; hay que mantener en sincronía proyección y eventos; los datos de una tarea quedan, como los mensajes, hasta que se purga el negocio.

**Qué haría que revisáramos esto:** que la proyección diverja de los eventos en producción, o que un pedido legal exija borrar también las tareas y los mensajes de un cliente (hoy solo se borra su memoria).

## Impacto en el código

- `core/kernel/state.py`; `core/orchestrator/loop.py` (`pending_confirmation` solo fuera de procedimiento).
- `core/llm/prompt_builder.py` (tarjeta en el bloque dinámico, antes de la fecha y hora) y un playbook del core con los rótulos.
- `core/memory/` (scopes, `memory_facts`, proyección en `customers.attributes`, `entity.py`, extensión de `purge_memory` y del cron de retención).
- Migraciones con RLS forzada y FKs compuestas. Las FKs compuestas exigen `UNIQUE (tenant_id, id)` en `conversations` y `customers`, creado con `CREATE UNIQUE INDEX CONCURRENTLY` + `ADD CONSTRAINT … USING INDEX` (tablas grandes).

## Adenda (2026-10-11, HU-AOS-36): memoria por scope, procedencia y olvido

Precisa §3 y §4 con lo que se implementó (migración `0080`, `core/memory/facts.py`).

- **Proyección solo de `perfil`.** `customers.attributes` proyecta únicamente los hechos del namespace `perfil`, con
  claves planas y el mismo algoritmo de antes (`{**attributes, **nuevos}`; `customers.name` si llega un `name` no
  vacío), así los lectores existentes (perfil del prompt, `get_customer_profile`, búsqueda de email de pack-calendar)
  ven exactamente lo mismo. Los demás namespaces (datos de tareas, `ventas`, `cobranza`…) viven solo en
  `memory_facts`: proyectarlos los metería en cada prompt saltándose `readScopes` y `data_class`. Los leerá el runtime
  v3 cuando exista su lector (HU-AOS-05/12). `perfil` es un namespace reservado: los hechos de una tarea nunca caen ahí.
- **Único escritor.** `core/memory/facts.py` es el único que escribe `memory_facts` y `customers.attributes`, en la
  misma transacción (un test estático lo vigila en `core/`, `packages/`, `cli/`, `scripts/` y `evals/`). El intake de
  leads de pack-commerce pasa por ahí (`entity.upsert_attributes_en_sesion`). Orden de locks: la fila del cliente
  primero (`FOR NO KEY UPDATE`), después sus hechos.
- **Backfill.** `0080` copia los `customers.attributes` de los clientes vivos como hechos `perfil` (`source = 'import'`),
  así la purga y la retención cubren lo que ya había. Con la hora de la migración: la edad real de cada clave no se
  conoce, y con la del cliente la primera retención borraría el perfil de clientes viejos pero activos. Lo que el
  código anterior escriba durante el deploy se completa con `agent-core tenant memoria-importar-perfil`.
- **`data_class`** la asigna el código por namespace y clave (raíces genéricas en español e inglés: nombre, documento,
  teléfono, dirección… → `pii`; salud, tarjeta, banco, claves → `sensitive`); si quien escribe declara otra, gana la más
  estricta. Los hechos no se embeben en `memory_vectors` (diseño §7.5: son estructurados).
- **Lo que escribe el LLM** (`source = 'agent'`, `update_customer_profile`): claves de una línea (`^[\w .-]{1,80}$`),
  valores de hasta 4 KB, hasta 20 claves por llamada, nada `sensitive` (pago, salud, credenciales, categorías
  especiales) y ninguna clave del sistema (`crm_lead_id`, `optin`, `source`). Si no, la tool devuelve un error
  genérico. Es la única conducta nueva sin bandera: antes ese dato quedaba en el perfil y en cada prompt.
- **Permisos.** `ToolContext.write_namespaces` (el dataclass del core, no el contrato congelado) lleva los
  `writeNamespaces` del agente v3; `None` = runtime legacy, sin chequeo. Con un conjunto, escribir fuera de él se
  deniega (`NamespaceNoConcedido`). El lector tolerante concede `["perfil"]` al General (lo que v2 ya podía escribir).
  Precondición de HU-AOS-14: el build de releases debe partir de versiones del General con ese valor (las que publicó
  el backfill B2 antes de esta adenda tienen `[]`). Los hechos que escribe la consolidación son del kernel (salen del
  motor de procedimientos, no de una escritura libre del agente) y no pasan por este chequeo.
- **Procedencia y consolidación.** Un cron nocturno (`core.memory_consolidation`, 03:30 UTC) pasa a `memory_facts` los
  datos de las tareas cerradas `completed` o `cancelled` por `episode_expired` de los últimos 7 días:
  - solo los confirmados o fijados por una acción, el kernel o una persona (no lo que el LLM extrajo sin confirmar) y
    nunca los de clase `payment`;
  - namespace = la habilidad del `procedure_id`; `source = 'human'` si lo fijó una persona y `'tool'` si no;
    `source_turn_id` = el turno del dato; `source_task_id` (columna nueva) = la tarea; `written_by_instance_id` = el
    dueño de la tarea;
  - corre fuera del turno (sin latencia ni fallos en la conversación) e idempotente: una tarea con hechos (aunque se
    hayan borrado) no se vuelve a mirar.
- **Olvido.** La purga por cliente (`purge_memory(customer_id=…)`) borra (lógico) en una transacción sus hechos, sus
  recuerdos de `memory_vectors` (también los intercambios del loop, que antes no tenían cliente y quedaban fuera) y deja
  `customers.attributes = '{}'` (todas sus claves son proyección); también los recuerdos sin cliente de sus
  conversaciones (los que el código anterior guardó durante el deploy); después del commit borra la memoria de trabajo de
  Redis de **todas** sus conversaciones (`wm:{tenant}:{conv}` y el legado `wm:{conv}`). `customers.name` y
  `contact_phone` no se tocan: son la identidad del cliente en el canal. `customers.memory_purged_at` (columna nueva,
  no mapeada en el modelo) marca la purga: la consolidación no toma tareas empezadas antes (y lo vuelve a mirar con
  el cliente bloqueado, por si la purga se confirmó en medio), para no resucitar lo olvidado. La CLI acepta
  `--customer-id` (los clientes de WhatsApp no tienen email como `external_id`).
- **Retención.** `MEMORY_RETENTION_DAYS` cubre `memory_facts` (por `created_at`, en lotes de clientes) y quita de la
  proyección las claves de los hechos vigentes que vence.
- **Intercambios fuera del recall.** El loop guarda cada intercambio sin `customer_id`; `store_memory` lo resuelve
  desde la conversación (filtro explícito de negocio), así esas filas cumplen `ck_mv_keys` y las cubre la purga. El
  recall excluye `kind = 'exchange'` y lo borrado, y sin cliente devuelve `[]`: con las banderas apagadas recuerda
  exactamente lo mismo que antes (los intercambios nunca se recordaban). Queda abierto si conviene seguir embebiendo
  intercambios que nadie lee.
- **Filas viejas sin cliente.** Los recuerdos que `0080` no puede asociar a un cliente (sin conversación, o de una
  conversación que no es del negocio) quedan `scope = 'tenant'` y borrados (lógico): nunca se recordaban y ninguna
  purga los alcanzaba.
- **Esquema.** Los CHECK `ck_mv_scope`, `ck_mv_kind` y `ck_mv_keys` quedan `NOT VALID` (se aplican a las filas nuevas);
  su `VALIDATE` va con el contract (HU-AOS-11/15), como los de `0070`. Igual las FKs compuestas nuevas de
  `memory_vectors` (`connection_id`, `agent_id`, `source_turn_id`, todas al mismo negocio); el backfill y
  `store_memory` solo copian una conexión que existe en el negocio (`conversations.connection_id` todavía no está
  validada).
- **Fuera de esta adenda:** resumen episódico (`kind = 'episode'`, requiere modelo), decaimiento de `importance`,
  re-embeber al cambiar `embedding_model`, recall en cadena por `readScopes`/`agent_id` y el lector v3 de hechos.
