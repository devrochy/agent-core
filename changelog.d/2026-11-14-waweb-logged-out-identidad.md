## Identidad de sesiones WhatsApp Web deslogueadas (2026-11-14)

- La detección de números duplicados ignora sesiones en `logged_out`: una fila
  sin credenciales ya no alerta «número en más de un negocio» en cada reinicio.
- Migración `0132`: limpia `jid`, `phone`, `push_name`, `business_name`,
  `platform` y `linked_at` de sesiones ya deslogueadas (backfill de `0130`).
  Conserva `last_connected_at` como historial.
