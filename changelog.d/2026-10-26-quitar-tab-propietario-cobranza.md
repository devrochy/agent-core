## Se quita el tab «Propietario» de Cobranza (2026-10-26)

- El hilo de mensajes con el propietario (reclamos y preguntas hacia SolversHub, ADR-0064) lo cubre ahora el
  asistente configurado. Se eliminan la sección de permisos `pack_cobranza:propietario`, los endpoints
  `GET /pack/cobranza/owner-thread` y `POST /pack/cobranza/owner-thread/messages`, y las rutas y plantilla HTML
  del admin viejo (`/cobranza/propietario`). Las tablas del hilo y las notificaciones de cobro se conservan;
  los permisos ya otorgados sobre esa sección quedan huérfanos y no tienen efecto.
