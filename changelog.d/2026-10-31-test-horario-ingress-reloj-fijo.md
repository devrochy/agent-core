## Test de horario de ingress con reloj fijo (2026-10-31)

- `tests/test_kernel_ingress_orden.py::test_dentro_de_horario_no_avisa` usaba la hora real con la franja `00:00`–`23:59` UTC. El fin de la franja es exclusivo, así que entre las 23:59 y las 00:00 UTC el negocio contaba como cerrado y el test fallaba (visto en agent-core#645, 23:59:23 UTC). Ahora evalúa el horario con `now` fijo a mediodía. Solo cambia el test; no hay cambio de comportamiento.
