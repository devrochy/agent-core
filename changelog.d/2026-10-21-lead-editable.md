## Clientes: estado e interés del lead editables desde la ficha (ADR-0149)

- `GET /api/v1/admin/leads/{id}` suma `editables`: los campos que un pack deja cambiar, con su valor.
- `PATCH /api/v1/admin/leads/{id}/pack/{plugin_id}` cambia esos campos; el core valida claves y valores
  (400 con `fields`) y responde 404 si el pack no está habilitado o no tiene edición.
- `LeadProvider` suma `edicion` (enmienda de ADR-0056). pack_commerce expone el estado y el interés del
  lead más reciente del cliente; si no tiene lead, lo crea sin datos de contacto.
- El perfil es la fuente del nombre, el teléfono y el correo del cliente: `crear_lead` y
  `actualizar_lead` también los escriben en él (su salida no cambia). El seguimiento de leads y la
  vista SSR saludan con el nombre del perfil.
