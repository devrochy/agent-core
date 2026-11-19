# ADR-0170 — Varias agendas por negocio

- **Estado:** propuesto (se acepta al mergear HU-CAL-08).
- **Fecha:** 2026-10-26
- **Contexto:** HU-CAL-08 (SPEC-24). El negocio quiere definir varios calendarios y que el cliente elija con quién agenda.
- **Enmienda:**
  - ADR-0084 (calendario interno: el solape se evalúa por agenda);
  - migración `0004` del pack (la restricción de exclusión pasa a incluir la agenda).
- **Relaciona:**
  - ADR-0158 (agenda por responsable: una agenda puede tener `responsable_admin_id`; HU-INMO-05 se reduce a asignar responsables y depende de esta);
  - ADR-0159 (agenda como capacidad del core).

## Contexto

`CalendarConfig` es una fila cifrada por negocio con un único `calendar_id`; `Appointment` y `CalendarBlock` no saben a qué agenda pertenecen. Los tokens de Google ya admiten varias cuentas en datos (`channel_connections`, único por cuenta) pero el runtime toma una sola y `save_tokens` podía heredar el `refresh_token` de otra cuenta.

## Decisión

1. **Una agenda es una fila de `pack_calendar_calendars`.** Una conexión de Google = una agenda (`connection_id`, único por negocio). Sin conexión es una agenda interna. Otro calendario de Google se agrega conectando otra cuenta.
2. **Nombre visible.** `name` (≤60, saneado: llega al modelo) es lo que el asistente le dice al cliente; `description` y `aliases` ayudan a mapear preferencias («la doctora», «sede norte»).
3. **Herencia.** `timezone` y `business_hours` en `NULL` heredan de `CalendarConfig` (que está cifrado y SQL no lo lee); `agendas.ensure_default()` completa en runtime lo que la migración no puede (la agenda principal toma la cuenta de Google ya conectada).
4. **Solape por agenda.** La exclusión pasa a `(tenant_id, coalesce(calendar_id, nil), tstzrange)`; el nombre nuevo conserva el viejo como prefijo para que el reconocimiento del error siga igual. `calendar_id` queda nullable (`NULL` = principal) hasta una migración «contract». El downgrade falla de forma explícita si ya hay solapes entre agendas.
5. **Bandera `multi_agenda_modo`** ∈ {`cliente_elige` (por defecto), `desactivado`}. La elección de agenda solo se ofrece con la bandera encendida y **2 o más agendas disponibles**; con una sola, las salidas de las tools son idénticas a las de antes y `list_calendars` no se expone.
6. **Tools.** `list_calendars` (con `agenda_anterior`: última cita no cancelada del cliente); `check_availability` y `find_next_available` aceptan `calendar` y `desired_time`, sin `calendar` consultan todas las agendas en paralelo (tope de 8 s por agenda; una caída se reporta sin romper el resto) y devuelven `libres_por_hora`, `disponibles_a_esa_hora` y alternativas; `create_calendar_event` sin `calendar` devuelve `needs_data` con las opciones. Modificar y cancelar usan la agenda de la cita; mover entre agendas queda fuera.
7. **OAuth por conexión.** Los tokens se leen y guardan por `connection_id`; una cuenta nueva sin `refresh_token` no hereda el de otra; reconectar con una cuenta distinta responde `otra_cuenta` sin sobrescribir. El espejo legacy en `plugin_configs` solo se escribe para la agenda principal.
8. **Panel.** Agenda es una sola vista (`/calendario`) con los ajustes en un cajón; la pestaña de Google pasa a la ficha de su conexión.

## Consecuencias

- Los esquemas de las tools cambian: las cassettes de evals que los incluyen se regraban con el LLM real (requiere OK del dueño).
- Con 4 colores categóricos, a partir de la 5ª agenda los colores se repiten.
- La reserva en Google sigue sin cerrar la carrera entre dos clientes (igual que antes); en agendas internas la cierra la restricción.
- La API suma `GET/POST /calendars`, `PUT /calendars/{id}`, `POST /calendars/{id}/delete`, `calendar_id` en citas y bloqueos, y `calendar_ids` en `/events`.
