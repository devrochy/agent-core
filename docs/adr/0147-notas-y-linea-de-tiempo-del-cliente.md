# ADR-0147 — Notas y línea de tiempo del cliente: tabla en el core, timeline armada al vuelo con los packs y notas en el bloque dinámico

- **Estado:** propuesto (HU-CRM-04, SPEC-33, D-40). Lo acepta el dueño en el PR.
- **Fecha:** 2026-10-21
- **Contexto:** SPEC-33 «CRM» en `solvershub-docs`. **Enmienda:** ADR-0056 (el `LeadProvider` suma
  `timeline`). **Usa:** ADR-0022 (el cliente es del core), ADR-0114 (memoria del cliente, tarjeta de
  estado con rótulos en un playbook del kernel y olvido), ADR-0053 (olvido), ADR-0091 (la fecha es la
  última parte del prompt), ADR-0119 (evals con grabación).

## Contexto

El dueño pidió ver la historia de cada cliente en un solo lugar y dejar notas que el equipo y el agente
tengan en cuenta. Hoy las notas son por conversación (`Conversation.state['notes']`, de la tool
`set_conversation_note` y del panel) y se pierden al abrir otra conversación. La historia está repartida
entre Conversaciones, Escalaciones, Pedidos, Agenda y la memoria del cliente. Ni SolversHub ni agent-sale
lo tienen.

## Decisión

1. **`customer_notes` en el core** (migración 0099). El cliente es del core y las notas cruzan packs:
   un pack de notas obligaría a otros packs a depender de él (invariante 5).
   - FK compuesta `(tenant_id, customer_id) → customers (tenant_id, id)`, RLS forzada, índice parcial
     `(tenant_id, customer_id, created_at DESC)` entre las vivas, texto de 1 a 2000 caracteres.
   - `author_admin_id → admins` (`ON DELETE SET NULL`) y `author_label` congelado al escribir.
   - Borra una nota su autor, el propietario o un admin de plataforma; el borrado es lógico.
   - **No** son `memory_facts` con namespace `notas`: los hechos son clave/valor con reemplazo
     («el nuevo cierra al anterior»), que no es la semántica de una nota.
2. **Línea de tiempo armada al vuelo**, sin tabla de historia (`core/admin/linea_de_tiempo.py`,
   `GET /admin/leads/{id}/timeline`).
   - Fuentes del core: conversaciones iniciadas, escalaciones (índice nuevo
     `escalations (tenant_id, conversation_id, created_at)`), datos aprendidos **incluidos los
     reemplazados** (se ve quién cambió qué) y notas del equipo.
   - **Enmienda de ADR-0056:** el `LeadProvider` suma `timeline(customer_id, cursor, limit)`. Un solo
     registro por pack para columnas, filtros y línea de tiempo: no hay un segundo registro que se
     desincronice. `resolver` pasa a ser opcional (pack_calendar solo aporta citas).
   - **Cursor compuesto** `(at, source, id)`, opaco para el cliente, y filtro `antes_del_cursor` en SQL
     por fuente: con varias fuentes hay empates de hora y un cursor solo por fecha perdería o repetiría
     eventos. Cada fuente trae `limit + 1` para saber si hay página siguiente.
   - Un pack que falla o tarda más de 3 s se omite y la respuesta sale `partial: true`.
   - Pedidos: su creación y su **último** cambio de estado (`status_changed_at`). No hay historia de
     estados guardada; `domain_events` es un outbox operativo (ADR-0107), no un registro. La historia
     completa sería otra HU.
   - Las notas por conversación (`Conversation.state['notes']`) **no** entran: no tienen fecha y no se
     pueden paginar en SQL. Siguen siendo notas de la conversación y no van al prompt.
3. **El agente lee las notas** en el bloque dinámico, después de lo que sabe del cliente y antes de la
   tarjeta de estado (la fecha sigue última, ADR-0091).
   - Rótulos e indicación en el playbook del kernel `notas-del-equipo` (invariante 3, mismo patrón que
     la tarjeta de ADR-0114 §2, fail-closed); `core/memory/notas.py` solo serializa, con cada texto
     escapado y delimitado por `tarjeta.escapar` (invariante 7).
   - Últimas 5 notas, tope de 1500 caracteres. **Sin notas no hay bloque**: el prompt no cambia y las
     grabaciones del gate siguen valiendo.
   - Lectura incondicional en v1: no se filtra por `readScopes` del agente. Una bandera nueva no tiene
     hoy un cliente que la pida.

## Consecuencias y riesgos asumidos

- **Olvido (ADR-0114 §4, ADR-0053):** `purgar_de_cliente` borra (lógico) también las notas, en la misma
  transacción que los hechos. `MEMORY_RETENTION_DAYS` no se aplica a las notas en v1: son registro del
  equipo, no memoria inferida.
- **Privilegio:** un colaborador con escritura en Clientes pero sin permiso sobre el agente puede poner
  texto en su contexto. Lo mitigan el escape, el rótulo de «dato, no instrucción» y el escenario de
  evals 36 (la nota trae una orden plantada y el agente no la sigue). Se asume.
- **Datos sensibles:** el texto libre no pasa por `facts.clasificar`, así que una nota con datos de salud
  llegaría al proveedor LLM. La ficha lo advierte al escribir. Se asume en v1; un filtro sería otra HU.
- **Evals:** escenario golden `36-notas-del-equipo` grabado con el LLM real (OK del dueño, 2026-10-21) y
  sumado al gate en replay.
