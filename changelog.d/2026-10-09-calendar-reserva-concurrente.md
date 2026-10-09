## Dos reservas a la vez ya no dejan una cita "agendada" sin fila (2026-10-09)

- HU-SH-02. Cuando dos clientes reservan el mismo horario a la vez, Postgres puede cortar uno de los
  inserts con `deadlock detected` en vez de con la restricción sin solape. El espejo lo tomaba como un
  error cualquiera y la tool respondía `created` sin `event_id`, y el cliente leía que su cita había
  quedado agendada. Ahora el insert se reintenta (hasta 3 veces) y, al reintentar, la base dice quién
  ganó. Si en modo interno igual no queda la fila, la tool devuelve un error en vez de confirmar.
  Lo encontró el CI (`test_dos_reservas_a_la_vez_una_gana_y_la_otra_recibe_alternativas`, intermitente).
