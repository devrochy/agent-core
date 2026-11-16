## Canal Telegram: alta, operación y salud (ADR-0199, PR core 2) (2026-11-15)

- `core/gateway/telegram/alta.py`: alta del bot del negocio con el token de @BotFather.
  - Valida el token con `getMe` (el `bot_id` sale de ahí).
  - Pide confirmación antes de pisar un webhook ajeno (409 `webhook_ajeno`).
  - **Reclama la conexión antes de `setWebhook`**: un bot vivo en otro negocio es 409 `bot_en_otro_negocio` sin pisar su webhook.
  - Registra el webhook con un secreto nuevo y lo confirma con `getWebhookInfo`. Si no confirma, responde 502 y deja la conexión en `error`; repetir el alta la recupera.
- `core/gateway/telegram/operacion.py`:
  - Salud: `degraded` si el webhook cambió, si hubo un error de entrega reciente o si hay más de 50 pendientes; `revoked` con un 401. Una caída de Telegram no cambia el estado.
  - Reparar el webhook.
  - Mensaje de prueba a un `chat_id`.
  - Cambio de token del mismo bot (otro bot es 409).
  - Desconectar con `deleteWebhook`.
- Rutas `/api/v1/admin/connections/telegram`: alta, lista, ficha, `refresh`, `webhooks`, `test-message`, `credentials` y `disconnect`. Con el kill-switch apagado responden 404, salvo desconectar. Las escrituras exigen ser propietario o master.
- Cron `core.telegram_health_check` cada 15 min, que no consulta a Telegram con el kill-switch apagado. Migración 0138 con `admin_telegram_connection_ids()`.
- Conexiones genéricas:
  - Borrar una conexión de Telegram borra también su fila.
  - Revelar credenciales, pausar o borrar un bot exige ser propietario o master.
  - El formulario manual no convierte un bot en otro tipo.
