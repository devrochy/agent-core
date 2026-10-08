## Demo agendada de punta a punta (2026-10-08)

- HU-SH-02. Sin doble reserva: restricción de exclusión (`btree_gist`) sobre las citas internas programadas de un
  negocio (migración `pack_calendar` `0004`); si dos prospectos eligen el mismo horario a la vez, uno queda y el otro
  recibe alternativas (también al reagendar y desde el panel, `horario_ocupado`).
- Confirmación con fecha, hora, zona horaria y enlace de la reunión (`AppointmentType.enlace_reunion` o el general
  `CalendarConfig.enlace_reunion`; texto `confirmacion_texto`).
- Recordatorios al cliente a 24 h y 1 h (`recordatorios_horas`, cron `recordatorios_de_citas` cada 15 min, una vez cada
  uno) por el outbox: plantilla si la ventana está cerrada (`appointment.reminder`).
- Aviso al equipo de cada cita nueva, reagendada o cancelada (`appointment.changed`): push al responsable del tipo de
  cita (`responsable_admin_id`, que la cita hereda en `asignado_admin_id`) o a los dueños.
- Asistencia: `POST /pack/calendar/appointments/{id}/asistencia` marca `asistio`/`no_asistio` una cita pasada; al
  `no_asistio` se le ofrece reagendar al cliente (`appointment.no_show`).
- Panel en modo interno: crear una cita con `customer_id` le avisa al cliente (`appointment.confirmed`) y al equipo.
  `PUT /pack/calendar/types/{id}` edita responsable y enlace.
