## HU-CONN-20 · Sandbox de Twilio, parte 3: webhook y operación por modo (2026-11-15)

- Webhook: el primer entrante o estado con firma válida y `AccountSid` correcto marca `webhook_confirmado_at` (un solo `UPDATE … WHERE webhook_confirmado_at IS NULL`) y pasa el sandbox de `pending` a `connected`. La firma no cambia: sigue fail-closed. Con `TWILIO_SANDBOX_PERMITIDO` apagada, un sandbox responde 200 sin procesar y no envía (`credenciales_de_envio` devuelve vacío).
- Salud de un sandbox: solo la cuenta (`GET /Accounts`) y la confirmación; nunca la Senders API.
- `POST /{id}/webhooks` en sandbox: 409 `sandbox_webhook_manual` con las dos URL. Rotar credenciales revalida la cuenta y no deja cambiar de cuenta (422 `sandbox_otra_cuenta`). Desconectar no restaura nada y responde `requiere_limpiar_consola=true`.
- Mensaje de prueba: el 63015 responde 422 `participante_no_unido` con el `join <código>` y el número del sandbox.
- Plantillas: crear, enviar a aprobación y borrar responden 409 `no_aplica_sandbox`; sincronizar sigue permitido.
