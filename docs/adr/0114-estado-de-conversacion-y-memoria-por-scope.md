# ADR-0114 — Estado de la conversación como tareas *event-sourced* y memoria por scope

- **Estado:** propuesto
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-04 y HU-AOS-36 (SPEC-26). Enmienda ADR-0053 (memoria, retención y PII).

## Contexto

- El estado de una conversación vive en un JSONB libre (`conversations.state`) y en el historial que el modelo relee en cada turno.
- La memoria tiene tres capas: L1 en Redis 24 h, L2 en `customers.attributes` y L3 en `memory_vectors` con redacción de PII.
- No hay una fuente única que diga qué está en curso ni qué datos ya dio el cliente.
- Con varios agentes, esa ausencia es la primera causa de incoherencia.

## Decisión

El estado de la conversación son tareas *event-sourced* y la memoria se separa por scope, con procedencia.

1. **Tareas *event-sourced*:**
   - `task_events` es append-only (`prevent_mutation`) y es la fuente de verdad.
   - `conversation_tasks` es la proyección, actualizada en la misma transacción y reconstruible.
   - Una tarea es una instancia de un procedimiento: etapa, datos validados, dueño, pila (una en foco y hasta `maxStack` en pausa).
2. **Tarjeta de estado:**
   - Se inyecta al final del bloque dinámico del prompt, sin romper la caché del prefijo.
   - Muestra la tarea en foco, los datos con su origen, lo que falta, lo pendiente de confirmar y las acciones de la etapa.
   - El panel "En curso" muestra lo mismo a las personas.
3. **Memoria por scope:**
   - tareas (estado estructurado), sesión, privada de un agente;
   - hechos del cliente con procedencia (`memory_facts`, escritura solo en los *namespaces* concedidos al agente);
   - episódica, semántica de la conexión y semántica del negocio.
   - **El estado gana** a cualquier recuerdo que lo contradiga.
4. **Olvido:**
   - `purge_memory` (ADR-0053) se extiende a `memory_facts` y a la caché del cliente, con borrado lógico (invariante 8).
   - Las tareas siguen la regla de `messages` (ADR-0053): son registro del negocio, la purga por cliente no las toca y solo `purge_tenant` las borra. `task_events` no tiene excepciones a su inmutabilidad.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Seguir con `conversations.state` en JSONB | Sin validación, sin historia, sin aserciones en evals |
| Solo el historial de mensajes | El modelo reconstruye el estado en cada turno y se equivoca |
| Eventos sin proyección | Cada lectura reconstruye; la latencia del turno sube |
| Redactar los valores de `task_events` en la purga por cliente | Exige mutar una tabla append-only (deshabilitar su trigger bloquea a todos los negocios) y el dato sigue en `slots`, `messages` y `audit_log` |

## Consecuencias

**Positivas:** un turno se puede reproducir y auditar; las evals afirman sobre el estado real; el relevo humano es inmediato; los datos no se piden dos veces.

**Negativas / lo que aceptamos pagar:** más tablas; hay que mantener en sincronía proyección y eventos; los datos de una tarea quedan, como los mensajes, hasta que se purga el negocio.

**Qué haría que revisáramos esto:** que la proyección diverja de los eventos en producción, o que un pedido legal exija borrar también las tareas y los mensajes de un cliente (hoy solo se borra su memoria).

## Impacto en el código

`core/kernel/state.py`; `core/llm/prompt_builder.py` (tarjeta en el bloque dinámico); `core/memory/` (scopes, `memory_facts` y extensión de `purge_memory`); migraciones con RLS forzada y FKs compuestas.
