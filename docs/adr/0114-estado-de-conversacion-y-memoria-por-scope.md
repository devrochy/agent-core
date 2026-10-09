# ADR-0114 — Estado de la conversación como tareas *event-sourced* y memoria por scope

- **Estado:** propuesto
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
