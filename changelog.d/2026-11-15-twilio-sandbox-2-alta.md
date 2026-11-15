## HU-CONN-20 · Sandbox de Twilio, parte 2: gate, alta y rutas (2026-11-15)

- `core/gateway/twilio/sandbox.py`: el único módulo que lee `TWILIO_SANDBOX_PERMITIDO` (lo vigila una prueba de arquitectura). Apagada, las rutas del sandbox responden 404.
- `alta.conectar_sandbox`: valida solo la cuenta (`GET /Accounts/{sid}`, sin Senders API), guarda `sandbox:<AccountSid>` con el número compartido como `From` y deja la conexión `pending` con `requiere_accion_manual` hasta el primer mensaje firmado. Es idempotente por cuenta, con el mismo candado advisory que el alta de senders. La misma cuenta en otro negocio da 409 `cuenta_en_otro_negocio`.
- API `/admin/connections/twilio`: `GET /capabilities` (`sandbox_disponible`, `sandbox_e164`), `POST /sandbox` (201/200) y `PATCH /{id}/sandbox` (`join_code`). Propietario o master con la bandera `canales.whatsapp_twilio`. `ConexionTwilioOut` suma `modo`, `sandbox_join_code`, `webhook_confirmado_at` y `requiere_accion_manual` (aditivo).
