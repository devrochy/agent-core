## Pausar el chat web desde la consola de conexiones (HU-AOS-23)

- **Consola de conexiones: el chat web se pausa y se reanuda desde ahí** (HU-AOS-23). `POST /admin/connections/{id}/action`
  acepta `activate` / `deactivate` sobre la conexión `webchat`, con el mismo interruptor que Configuración → Chat web
  (`app_settings` y `channel_connections.active` en una transacción) y la misma auditoría `connection.paused` /
  `connection.resumed`, que ahora también deja Configuración al cambiar el interruptor. La fila del chat web sale con
  `pause_scope = "connection"` y `pausable = true`. ADR-0122, adenda 2026-10-19 (b).
