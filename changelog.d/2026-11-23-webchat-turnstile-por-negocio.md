## Chat web con Turnstile por negocio (2026-11-23)

- `PUT /api/v1/admin/settings/web-chat` acepta `turnstile_site_key` además de `turnstile_secret`:
  o llegan las dos o ninguna. Media configuración responde `422 turnstile_incompleta` porque el
  widget no podría pedir el token y todos los mensajes terminarían en `403`.
- `GET /api/v1/admin/settings/web-chat` devuelve la site key pública (`turnstile_site_key`); la
  secret key sigue sin salir nunca de la API.
- `GET /api/v1/public/web-chat/{key}/config` devuelve `turnstile_site_key` para que el widget pueda
  renderizar el desafío de Cloudflare. `webchat_resolve` (migración 0159) la lee de
  `channel_connections.settings`; el secreto sigue cifrado en `credentials_encrypted`.
- La conexión `webchat` guarda la site key en `settings` y el secreto cifrado como antes, así que
  el backfill y la doble escritura con `app_settings` siguen funcionando.
