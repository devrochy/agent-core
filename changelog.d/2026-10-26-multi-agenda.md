## Agendamiento: varias agendas por negocio (2026-10-26)

- HU-CAL-08 / ADR-0170. Una conexión de Google = una agenda; también hay agendas internas. Cada
  agenda tiene nombre visible (el que el asistente le dice al cliente), descripción, alias, color,
  zona y horario opcionales (heredan del negocio).
- Migración `0006` del pack: tabla `pack_calendar_calendars` (RLS forzada), `calendar_id` en citas y
  bloqueos con backfill a «Agenda principal», y solape por agenda. Reversible; el downgrade falla si
  ya hay solapes entre agendas.
- Nueva bandera `multi_agenda_modo` (`cliente_elige` | `desactivado`): la elección de agenda solo
  aplica con 2 o más agendas. Con una, las tools responden igual que antes.
- Tools: `list_calendars`; `calendar` y `desired_time` en `check_availability` y
  `find_next_available` (disponibilidad por agenda y por hora deseada); `calendar` en
  `create_calendar_event` (sin él, `needs_data`). Playbook `citas` con la sección «Varias agendas».
- API `/api/v1/pack/calendar`: `GET/POST /calendars`, `PUT /calendars/{id}`, `POST /calendars/{id}/delete`,
  `calendar_id` en citas y bloqueos, `calendar_ids` en `/events`, `/oauth/start?connection_id=`.
- OAuth de Google por conexión: una cuenta nueva ya no hereda el `refresh_token` de otra y reconectar
  con otra cuenta responde `otra_cuenta` sin pisar la conexión.
- Pendiente (requiere OK del dueño): regrabar la cassette `evals/cassettes/journeys/suscripciones/01-informar-planes.json`
  (cambió el esquema de las tools) y aplicar la migración en test.
