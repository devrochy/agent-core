## HU-CONN-20 · Sandbox de Twilio, parte 1: esquema y configuración (2026-11-15)

- Migración 0136 (aditiva): `twilio_connections` suma `modo` (`sender|sandbox`), `sandbox_join_code` y `webhook_confirmado_at`; `sender_sid` admite NULL solo en modo sandbox (CHECK). Índice único parcial de un sandbox vivo por cuenta en cada negocio. La bajada falla si existe alguna conexión sandbox, viva o borrada.
- `Settings.twilio_sandbox_permitido` (`TWILIO_SANDBOX_PERMITIDO`, apagada por defecto) y `twilio_sandbox_e164` (`+14155238886`). Todavía no se leen: las rutas y el gate llegan en la parte 2.
- `save_connection_in` acepta `external_id_override` (el sandbox se guarda como `sandbox:<AccountSid>`).
- channel-twilio: el error 63015 (participante no unido al sandbox) es `PERMANENT`: no se reintenta y no da de baja al cliente.
