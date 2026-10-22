## Perfil propio: nombre, teléfono, usuario, contraseña y avatar (SPEC-34)

- `GET /admin/me` incluye `nombre`, `telefono` y `avatar_url` (URL firmada resuelta desde la
  `key` del almacenamiento).
- `PATCH /admin/me` actualiza nombre, teléfono y usuario (único, con validación).
- `POST /admin/me/password` cambia la contraseña pidiendo la actual; `POST/DELETE
  /admin/me/avatar` suben y quitan la foto por el controlador único (perfil `avatar`, 2 MB).
- `CollaboratorItem.avatar_url` también resuelve la URL firmada para la sección Equipo.
