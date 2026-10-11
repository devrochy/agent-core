## Memoria por scope con procedencia y olvido (2026-10-10)

HU-AOS-36 (agent-core #398, ADR-0114 y su adenda, ADR-0053). Sin cambio de conducta con las banderas apagadas: el
perfil del cliente queda idéntico y el recall también.

- **Hechos con procedencia.** `memory_facts` es la fuente del perfil del cliente y `customers.attributes` su
  proyección (solo el namespace `perfil`), en la misma transacción. El único escritor es `core/memory/facts.py`;
  `entity.upsert_attributes` y el intake de leads de pack-commerce pasan por ahí. Cada hecho guarda fuente, turno,
  tarea, instancia, confianza y `data_class` (`pii`/`sensitive` por namespace y clave). Un valor nuevo cierra al anterior
  (`superseded_at`); el mismo valor no crea fila.
- **Lo que escribe el LLM** con `update_customer_profile`: claves de una línea, valores de hasta 4 KB, nada de pago,
  salud ni contraseñas, y no pisa claves del sistema (`crm_lead_id`, `optin`, `source`). Si no, error genérico.
- **Permisos.** `ToolContext.write_namespaces` (None en el runtime legacy): un agente v3 sin el namespace concedido no
  escribe. El lector tolerante concede `perfil` al General.
- **Consolidación nocturna** (`core.memory_consolidation`, 03:30 UTC): los datos confirmados de las tareas
  `completed` y de las cerradas por `episode_expired` pasan a hechos con su tarea y su turno. Nunca los de pago ni lo
  que el LLM extrajo sin confirmar.
- **Olvido.** `purge_memory(customer_id=…)` (CLI `tenant memoria-purgar --customer-id` o `--email`) borra (lógico) los hechos del
  cliente, su perfil proyectado y sus recuerdos, incluidos los intercambios del loop, que antes no tenían cliente y la
  purga no alcanzaba. También limpia la memoria de trabajo de Redis de todas sus conversaciones. Sus tareas y mensajes
  quedan. Lo olvidado no vuelve por la consolidación (`customers.memory_purged_at`).
- **Retención.** `MEMORY_RETENTION_DAYS` cubre también `memory_facts` y su proyección.
- **Recall.** Ignora lo purgado y los intercambios (como antes), y sin cliente no devuelve nada.
- **Migración `0080`** (sobre `0076`; se re-encadena al mergear). Agrega:
  - en `memory_vectors`: `scope`, `connection_id`, `agent_id`, `kind`, `importance`, `expires_at`, `embedding_model`
    y `source_turn_id`, con los CHECK `NOT VALID`;
  - `memory_facts.source_task_id`;
  - `customers.memory_purged_at`;
  - los índices concurrentes `ix_memory_vectors_scope` e `ix_conversation_tasks_closed`.

  Además, backfills en lotes: el cliente y la conexión de los intercambios viejos, y los hechos `perfil` desde
  `customers.attributes` (clientes vivos, con la hora de la migración). Los recuerdos viejos sin cliente posible quedan
  borrados (lógico). Exige el rol dueño (`BYPASSRLS`).
- **Después del deploy:** `agent-core tenant memoria-importar-perfil` (copia lo que el código anterior escribió en
  `attributes` mientras tanto; idempotente) y `VACUUM (ANALYZE) memory_vectors`.
