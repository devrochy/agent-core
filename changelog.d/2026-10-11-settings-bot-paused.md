## API: pausar o reactivar el asistente sin reenviar la identidad (2026-10-11)

- Nuevo `PUT /api/v1/admin/settings/bot-paused` con cuerpo `{"paused": bool}`: cambia solo
  `app_settings.bot_paused` del negocio activo (exige escritura en `settings`). Fija el valor pedido, así
  que repetirlo no cambia nada, y registra `settings.bot_paused` / `settings.bot_resumed` en `audit_log`
  cuando el valor cambia.
- El panel (HU-UI-03) lo usa para la pausa de la barra superior. Antes leía la configuración y reenviaba
  la identidad completa por `PUT .../identity`, así que dos personas editando a la vez se pisaban
  (ganaba la última escritura).
