## La agenda sale a Google solo por el cliente acotado del core (2026-10-08)

- `pack-calendar` dejó de usar `httpx` directo (lo señaló el auditor multi-tenant en HU-SH-02). Las tools
  (`create_event`, `update_event`, `cancel_event`, `check_availability`/`find_next_available`) llaman a
  Google Calendar y refrescan el token por `context.http`. El panel (`admin_api`, `admin`) y el OAuth, que no tienen
  `ToolContext`, usan `plugin_http` con la misma allowlist. Todo vive en `pack_calendar/google_api.py`.
- Core: `BaseModule.network_allowlist` declara los dominios del pack (agenda: `www.googleapis.com`,
  `oauth2.googleapis.com`), y `plugin_manager.scoped_http(plugin_id)` arma con ellos el `context.http` de sus tools.
  Antes las tools de un pack recibían una allowlist vacía. `ScopedHttp` acepta `methods`, `data` (form) y `timeout`
  por llamada. Para packs: `GET/POST/PUT/PATCH/DELETE`. Las tools declarativas del tenant siguen limitadas a GET/POST.
- Una tool registrada ya no toma la allowlist de una tool declarativa con el mismo nombre.
- Sin cambios de comportamiento: timeout de 30 s y errores como `PluginError("Calendar API <status>")`. El único
  cambio es que la cancelación desde el panel ahora deja un warning en el log también cuando Google responde 4xx.
