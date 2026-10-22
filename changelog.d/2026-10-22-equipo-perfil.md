## Equipo: perfil del colaborador, listado por negocio y alta en dos pasos (SPEC-34)

- `admins` gana `nombre`, `telefono` y `avatar_url` (migración 0102). El avatar se editará desde
  el perfil del propio usuario; la API ya expone el campo.
- `GET /admin/collaborators` devuelve **solo el negocio activo**; el master puede pedir
  `?todos=1` para ver todas las cuentas de la plataforma. `CollaboratorItem` incluye el perfil.
- Endpoints nuevos `GET /admin/collaborators/existe?email=` y
  `GET /admin/collaborators/username-disponible?username=` para el alta en dos pasos.
- El alta guarda `nombre` y `telefono`, valida el `username` con un error claro y, sin slug, cae
  en el negocio activo (el negocio deja de elegirse).
