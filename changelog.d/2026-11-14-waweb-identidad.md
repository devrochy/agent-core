## Identidad del vínculo WhatsApp Web (2026-11-14)

- El vínculo por QR deja de mostrar `numero: User`: el JID se extrae del proto de
  Neonize (no de `str(proto)`) y se guarda normalizado como `<numero>@s.whatsapp.net`.
- Se persiste la identidad visible del teléfono (`phone`, `push_name`, `business_name`,
  `platform`) y la fecha de primera vinculación (`linked_at`), que se conserva al
  reconectar y se limpia al re-vincular o cerrar sesión.
- La ficha de la conexión y la fila de Conexiones del panel muestran nombre, número,
  negocio, dispositivo y fechas. La APK muestra nombre y número en Configuración →
  Conexiones.
- Migración `0130`: agrega las columnas y normaliza los JID viejos (`User: "..."`);
  `linked_at` se rellena desde `last_connected_at` como mejor aproximación.
- `GET /admin/meta/waweb`, `GET /admin/connections` y `GET /admin/connections/console`
  exponen los nuevos campos.
