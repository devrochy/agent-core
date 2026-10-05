# Calendario interno: configuración y uso (SPEC-24)

Guía breve del calendario interno del pack `pack_calendar` (agenda propia, sin
Google) y del agendamiento con el asistente. Ver ADR-0084 y las HUs
HU-CAL-01/02/03.

## Modos de agenda

`CalendarConfig.source`:

- **`interno`** (por defecto): la agenda vive en la base de datos
  (`pack_calendar_appointments`, `pack_calendar_blocks`). No requiere Google.
- **`google`**: crea/mueve/borra el evento en Google Calendar y lo espeja local.

En modo interno, la disponibilidad se calcula contra el **horario de atención**
(`business_hours`) menos las citas agendadas y los bloqueos.

## Configurar (panel → Calendario)

1. **Habilitar** la habilidad Calendario (Habilidades → Calendario).
2. **Conexión**: `source = interno`, zona horaria (`America/Bogota`), duración por
   defecto, tamaño de bloque (`slot_minutes`) y margen entre citas
   (`buffer_minutes`).
3. **Horario de atención**: días y horas en que se puede agendar (`""` = cerrado).
4. **Tipos de cita**: nombre + duración (p. ej. Demo 30 min, Asesoría 45 min).
5. **Bloqueos**: días, franjas o almuerzo en que NO se agenda ("fuera de oficina").

## Correo de agendamiento (HU-CAL-02)

El **servidor** envía el correo de confirmación/reagenda/cancelación de forma
determinista (no depende de que el modelo lo mande). Destinatarios:

- El **cliente** (`attendee_email` de la cita).
- Los **colaboradores con el permiso `pack_calendar:notificaciones`** (se otorga
  en Colaboradores). El owner siempre cuenta.

Se envía por la conexión **SMTP** del negocio (`email_smtp`, ADR-0071). Si no hay
conexión o email, la acción se completa igual y queda registrada con
`email_sent=false` y el motivo. Plantillas configurables por tenant
(`email_subject_*`, `email_body_*`; placeholders `{cliente}`, `{titulo}`,
`{fecha}`, `{hora}`, `{negocio}`, `{link}`). Si la plantilla no trae `{link}`,
se agrega al final la línea "Agregá la cita a tu calendario: …" (salvo al
cancelar). Un placeholder mal escrito queda literal: nunca tumba el aviso.

## Link de la cita (HU-CAL-05)

Al agendar o reagendar, el cliente recibe un link "agregar a mi calendario"
(plantilla de Google Calendar: `calendar.google.com/calendar/render?action=TEMPLATE…`
con la hora local y `ctz`), en la respuesta del asistente y en el correo. No
requiere Google conectado: sirve en modo interno.

## Uso con el asistente

- **Fecha actual**: el asistente recibe la fecha y hora del negocio en cada
  turno (ADR-0091). `check_availability` con una fecha pasada devuelve
  `in_past` (con `today`) y para hoy no ofrece horas que ya empezaron.
- **Sin fecha**: `find_next_available` (`days` 1-14, por defecto 7) devuelve
  los próximos días con horarios libres, salteando días cerrados y sin cupo
  (máximo 5 días).
- **Agendar**: consulta disponibilidad (`check_availability`) y crea la cita
  (`create_calendar_event`) tras confirmar datos y horario.
  - **El email del cliente es obligatorio** y debe ser uno que el cliente haya
    escrito en la conversación (o que ya esté en su perfil): si falta, la tool
    devuelve `needs_data` y **no crea**; el asistente lo pide. **Nunca se acepta
    un email inventado por el modelo.**
- **Reagendar**: `update_calendar_event` (mueve, no duplica).
- **Cancelar**: `cancel_calendar_event`. Acepta el `event_id` real de la lista,
  el **número de la lista** (`event_id: "2"` = 2ª cita futura) o `cancel_all`.
  Un id no reconocido con varias citas **no cancela a ciegas**: devuelve
  `not_found` y el asistente pide aclaración.
- **Validación dura**: el servidor rechaza solapamientos (`occupied`), horarios
  fuera de atención (`outside_hours`) y fechas pasadas (`in_past`): no hay doble
  reserva.

## Panel (HU-CAL-03)

`/calendario/citas` muestra la grilla tipo Google Calendar (mes/semana/día):
arrastrar para mover, estirar para cambiar la duración (con confirmación de
aviso), clic en un hueco para crear, detalle con historial de actividades, y los
bloqueos en la misma grilla.

## API (panel nuevo)

- `GET /api/v1/pack/calendar/events?start&end` — citas + bloqueos (FullCalendar).
- `POST /api/v1/pack/calendar/appointments` — crear.
- `PATCH /api/v1/pack/calendar/appointments/{id}` — editar/mover (`notify`): al
  mover con aviso, correo + WhatsApp al cliente; la respuesta trae `notification`
  (`email_sent`, `email_reason`, `whatsapp_sent`).
- `POST /api/v1/pack/calendar/appointments/{id}/cancel` — cancelar (body opcional
  `{"notify": true}`): registra `cancelada`, avisa por correo + WhatsApp y
  devuelve `{"status": "cancelled", "notification": …}`. Idempotente.
- `GET /api/v1/pack/calendar/appointments/{id}/activities` — historial.
- `GET/POST/DELETE /api/v1/pack/calendar/blocks`, `PATCH /blocks/{id}` — bloqueos.

## Migraciones

- `0002_calendar_interno`: `pack_calendar_blocks` + asegura tablas del pack.
- `0003_calendar`: `created_by` (`assistant|panel|customer`) y `timezone` por cita.

## Guardrail de confirmaciones (HU-CAL-05)

`calendar_completion_check` exige evidencia real del turno: "quedó agendada"
solo con `create_calendar_event` en `created` (o, sin create, con
`list_client_appointments` OK); "moví/reagendé" con `updated`; "cancelé" con
`cancelled`; "te envié el correo" con `send_email` OK o `email_sent: true` del
create/update/cancel. Decir "no pude…" exige haberlo intentado. Las preguntas
("¿te agendo la demo?") no cuentan como afirmación. El reintento nunca pide
`send_email` y le indica al modelo no mencionar la instrucción al cliente.
