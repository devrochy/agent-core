## El asistente ya no niega una cita de hoy que ya pasó (2026-10-10)

- En test, a las 23:49 de Bogotá una clienta preguntó por su cita de las 10:00 de ese día, que seguía
  en `scheduled`. `list_client_appointments` solo miraba las citas futuras: devolvió `total: 0` tres
  veces y el asistente dijo que no aparecía ninguna cita.
- Ahora la tool también devuelve las citas de las últimas 24 h que ya pasaron, con `ya_paso: true` y
  sin índice; las futuras llevan `ya_paso: false` y conservan la numeración 1..N que usa
  `cancel_calendar_event` ("cancelá la 2"). La respuesta suma `proximas` (cuántas faltan) y `total`
  cuenta todas.
- La descripción de la tool le pide al asistente que diga que la cita ya pasó, con su fecha y hora,
  que ofrezca agendar otra y que nunca niegue una cita marcada `ya_paso`.
