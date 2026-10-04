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
`{fecha}`, `{hora}`, `{negocio}`).

## Uso con el asistente

- **Agendar**: consulta disponibilidad (`check_availability`) y crea la cita
  (`create_calendar_event`) tras confirmar datos y horario.
- **Reagendar**: `update_calendar_event` (mueve, no duplica).
- **Cancelar**: `cancel_calendar_event`.
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
- `PATCH /api/v1/pack/calendar/appointments/{id}` — editar/mover (`notify`).
- `POST /api/v1/pack/calendar/appointments/{id}/cancel` — cancelar.
- `GET /api/v1/pack/calendar/appointments/{id}/activities` — historial.
- `GET/POST/DELETE /api/v1/pack/calendar/blocks`, `PATCH /blocks/{id}` — bloqueos.

## Migraciones

- `0002_calendar_interno`: `pack_calendar_blocks` + asegura tablas del pack.
- `0003_calendar`: `created_by` (`assistant|panel|customer`) y `timezone` por cita.
