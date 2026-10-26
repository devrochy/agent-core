# ADR-0167 — Centro de notificaciones del negocio (feed in-app y push)

- **Estado:** propuesto (se acepta al mergear la HU del núcleo).
- **Fecha:** 2026-10-25
- **Contexto:** SPEC de notificaciones de solvershub-docs. **Enmienda:** ADR-0099 (push por Expo: destinatarios, textos y disparo) y ADR-0107 §1 (el outbox de eventos de dominio carga un consumidor de notificaciones). **Usa:** ADR-0136 (permisos por sección), ADR-0107 (outbox).

## Contexto

Hoy el negocio se entera de lo que pasa por un push que solo llega a los dueños (ADR-0099) y sale de once disparadores sueltos, con `data.type` y `data.kind` mezclados. No existe un feed: nada guarda lo enviado, no hay contador ni «leída», ni preferencias, y los colaboradores con permiso sobre una sección no se enteran de nada. Faltan además eventos del negocio (lead nuevo, pedido creado, conexión caída). El panel y la app necesitan una campana con historial; el push debe ser el mismo aviso, no un camino aparte.

## Decisión

1. **Una fila por evento, estado de lectura aparte.** `notifications` (una fila por evento del negocio) más `notification_reads` (solo lo leído o archivado por cada admin) y `notification_state` (una fila por admin y negocio: marca «leído hasta», silencio y categorías con el push apagado; sin fila = todo activo). Las tres son tablas de negocio con RLS forzada y baja lógica (invariantes 1 y 8). No se crea una fila por destinatario: la tabla no crece con el equipo, un cambio de permisos se refleja al instante y el master funciona sin fila en el negocio.
2. **Visibilidad por permiso de sección, al leer.** El dueño y el master (o tester habilitado) del negocio seleccionado ven todo. Un colaborador ve los eventos cuya `section` tenga acceso **explícito** (`read` o `write`; sub-sección > pack; `none` deniega): heredar `read` por el rol no da avisos, igual que `notification_recipients`. Un evento sin `section` es solo de dueños y masters.
3. **Los packs son dueños de sus textos.** Core define los enums (categorías `conversaciones`, `ventas`, `cobros`, `agenda`, `sistema`; severidades `info`, `success`, `warning`, `critical`) y la `NotificationSpec`, y la valida. El catálogo de textos en core cubre solo eventos de core (escalación, conexión, mensajes fallidos). Ningún pack importa a otro (invariante 5). Los textos son constantes de código (infraestructura determinista, no prompts: invariante 3 no aplica, como en ADR-0099) y `data` lleva ids, nunca PII. El destino es neutro, `{entity, id, section}`: cada cliente lo traduce a su ruta.
4. **Emisión que no rompe el hecho.** `notify(session, tenant_id, spec)` inserta la fila y escribe `notification.created` en el outbox en un SAVEPOINT y **nunca propaga**: un texto mal armado no puede revertir un pago. `notify_standalone` abre su propio scope para los disparadores fuera de una transacción. Los packs llaman `notify()` desde sus handlers actuales; no hay un adaptador central que lea payloads de packs.
5. **Push idempotente.** Un handler core de `notification.created` (despachador del outbox, at-least-once) resuelve destinatarios (con dispositivo, que la vean, sin silencio ni la categoría apagada), envía en lotes de 100 con `badge`, `priority`, `ttl` y canal de Android por categoría, y marca `push_sent_at` solo al terminar. Un reintento del outbox no repite lo entregado. El despachador corre cada 30 s (ADR-0107); un disparador crítico puede invocar el mismo envío tras su commit y `push_sent_at` lo hace idempotente.
6. **Dedupe por episodio.** La clave de un evento recurrente incluye el episodio (p. ej. conexión y estado desde), para no repetir el aviso en cada health check ni recrearlo si el usuario lo archivó.
7. **API propia del admin** `/api/v1/me/notifications`: lista con cursor opaco y filtros, contador, marcar leídas (ids o todas), archivar, preferencias de push y silencio. Sin gate de sección: son filas propias y se filtran por visibilidad. Tiempo real por polling del contador; SSE queda para después.
8. **Retención.** Baja lógica a los 180 días, leída o no, en lotes por negocio (patrón de `memory_retention`). Sin borrado físico.
9. **Alcance.** Quedan fuera los avisos de facturación de la plataforma al vendedor (renovación, conciliación, pago doble, aprovisionamiento): siguen con `notify_owners_push`. El feed pull del gestor de cobranza (`GET /gestor/notificaciones`) convive sin cambios.

## Enmiendas

- **ADR-0099:** los destinatarios dejan de ser solo los dueños (ahora, quien ve la notificación y tiene dispositivo), los textos viven en el emisor de cada evento, y el disparo pasa por `notify()` en lugar de `notify_owners_push` desde cada sitio. La tabla `push_tokens` y su registro no cambian. El silencio y las preferencias son **por negocio**: quien tiene varios negocios los configura en cada uno, y su dispositivo solo está registrado en el negocio activo (límite heredado de ADR-0099).
- **ADR-0107 §1:** además de los handlers de avisos al cliente, el outbox carga `notification.created`. El push del centro tiene la latencia del despachador (≤ 30 s) salvo el atajo post-commit de los eventos críticos.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Una fila por destinatario | Crece con el equipo, hay que resolver el reparto al emitir, un cambio de permisos no se refleja en lo ya enviado y el master no tiene fila. |
| Catálogo central de textos de todos los packs en core | Mete lógica de dominio de cuatro packs en core y exige tocarlo con cada pack nuevo. |
| Adaptador en core que lee los payloads de `payment.*`, `appointment.*`… | Acopla core a payloads que no están en `@agentcore/contracts`, y un fallo de notificaciones frenaría el evento de pago (`emit_strict`). |
| Preferencia «in-app por categoría» | Ningún cliente la pide y complica el contador; el feed es el registro y siempre muestra. |
| SSE o WebSocket desde el inicio | No hay infraestructura de tiempo real hacia el panel; el polling del contador basta para v1. |

## Consecuencias

**Positivas:** un solo aviso para panel, app y push; colaboradores con permiso se enteran; el push es idempotente y no puede romper un cobro; la tabla no crece con el equipo.

**Negativas / lo que aceptamos pagar:** la visibilidad se calcula al leer (consulta de secciones presentes más filas explícitas del admin); un lote de más de 100 dispositivos que falle a mitad puede repetir el primer lote al reintentar; las notificaciones viejas de una sección a la que el usuario perdió acceso dejan de verse (se filtra por permiso vigente); migrar los disparadores obliga a actualizar más de diez tests que parchean `notify_owners_push`.

**Qué haría que revisáramos esto:** necesidad de tiempo real por debajo del polling, de preferencias por canal o por evento, o de notificar a personas sin cuenta de admin.

## Impacto en el código

- `core/db/models.py` (`Notification`, `NotificationState`, `NotificationRead`), `alembic/versions/0107_notificaciones.py`.
- `core/notifications/` (`spec.py`, `service.py`, `feed.py`; `push.py` con lotes, `badge`, `priority`, `ttl` y canal por categoría), `core/events/core_handlers.py` (handler de `notification.created`), `core/shared/events.py` (topic).
- `core/api/routers/notifications.py`, `core/tasks/notifications_retention.py` (+ beat en `celery_app`), `docs/openapi.json`.
- Disparadores migrados a `notify()` en la HU siguiente.
