## Agenda unificada: color y código por tipo de cita (HU-UX-08, SPEC-12)

- `pack_calendar_appointment_types` suma `color` (clave de la paleta categórica `cat-1`…`cat-4` de SPEC-29,
  con CHECK: nunca un hex libre) y `codigo` (máx. 6, p. ej. «VAL»). Migración `pack_calendar` 0005,
  reversible; las filas existentes reciben un color rotando la paleta por negocio y el código de las 3
  primeras letras del nombre.
- `GET/POST/PUT /api/v1/pack/calendar/types` exponen `color` y `codigo`. Al crear sin color, rota la paleta;
  sin código, sale del nombre. Al editar, no mandarlos los conserva (clientes viejos como la app).
- `GET /api/v1/pack/calendar/events`: cada cita trae `tipo_color` y `tipo_codigo`, y la respuesta trae
  `tipos` (id, nombre, color y código de los tipos activos) para la leyenda del calendario.
- Mover un bloqueo (inicio y fin) ya existía en `PATCH /blocks/{id}`; queda cubierto con un test
  `aislamiento`, igual que los tipos con color.
