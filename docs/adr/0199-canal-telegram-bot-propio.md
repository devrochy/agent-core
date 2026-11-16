# ADR-0199 — Canal Telegram, bot propio por negocio

- **Estado:** propuesto
- **Fecha:** 2026-11-15
- **Contexto:** D-74 (docs); HU-CONN-22 a HU-CONN-28 (SPEC-08). Decisiones del dueño del 2026-11-15.
- **Enmienda:** ADR-0197 punto 6 (la voz deja de estar atada a `waweb`, §13).
- **Usa:** ADR-0007 (ids globales de mensajes), ADR-0052 (canales por entry points), ADR-0069 (catálogo de tipos de conexión), ADR-0094 (credenciales por conexión), ADR-0115 (conexión canónica), ADR-0122 (pausa y borrado lógico), ADR-0186 (WhatsApp vía Twilio: el precedente de alta, webhook por `public_id`, salud y rutas), ADR-0188 (observabilidad), ADR-0197 (respuestas por voz).

## Contexto

El agente atiende por WhatsApp (Meta, Twilio y QR), Instagram, Messenger, chat web y correo. Telegram no cobra los mensajes, no tiene ventana de 24 h ni plantillas y no depende de que Meta verifique a solvershub. El contrato de canal (`core/gateway/channels/base.py`) ya nombra a Telegram como el siguiente canal, `ChannelKind` ya incluye `"telegram"` y `Customer.external_id` es texto libre con `contact_phone` opcional.

Dos hechos de la Bot API (https://core.telegram.org/bots/api, v10.4, leída el 2026-11-15) dan forma al esquema:

- `setWebhook.secret_token` (1-256 caracteres, `A-Za-z0-9_-`) llega en cada webhook en el encabezado `X-Telegram-Bot-Api-Secret-Token`.
- `update_id` es «unique identifier» y crece de a uno, pero tras una semana sin updates el siguiente se elige al azar. La documentación no dice «por bot». Tampoco dice que el número antes de `:` en el token sea el id del bot.

## Decisión

**Telegram es un canal propio (`channel='telegram'`, `provider='telegram'`, tipo `telegram`) con un bot por negocio: el negocio lo crea en @BotFather y pega el token; el backend lo valida, registra el webhook y vigila la salud. Está disponible para todos los negocios, sin bandera por negocio, con un kill-switch de entorno.**

1. **Conexión.** Va en `channel_connections` con `external_id = bot_id`. El `bot_id` sale de `getMe.result.id`, nunca del token. `credentials_encrypted` (AES-GCM) lleva `{bot_token, bot_id, webhook_secret}`. Una tabla 1:1, `telegram_connections` (migración 0137), guarda lo que no es secreto:
   - `public_id` aleatorio (`token_urlsafe(18)`, 144 bits), único: va en la URL del webhook;
   - `bot_username` y `bot_name`;
   - `status` (`pending`, `connected`, `degraded`, `revoked`, `error`);
   - `webhook_url`, `webhook_confirmado_at`, `pending_update_count`, `last_error_message/at`, `last_update_at` y `last_checked_at`.

   Lleva FK compuesta `(tenant_id, channel_connection_id)`, RLS forzada y sin DELETE para `app_runtime`, como 0121. El `bot_id` NO se duplica en la tabla: la fuente de verdad es `channel_connections.external_id`.
2. **Un bot, un negocio.** Índice parcial único GLOBAL `uq_channel_connections_telegram_bot_viva` sobre `channel_connections (external_id) WHERE provider='telegram' AND deleted_at IS NULL`. Un bot solo admite un webhook. El alta traduce el `IntegrityError` a 409 `bot_en_otro_negocio` sin decir cuál; el borrado lógico libera el bot.
   - Es un índice que no empieza por `tenant_id`: una excepción deliberada a la invariante 1, como `uq_channel_connections_twilio_sender_viva` (0121). El 409 revela que el bot está en otro negocio, pero solo a quien ya tiene su token.
   - No hay una función que mire otros negocios. En Twilio, `twilio_account_en_otro_negocio` existe porque la cuenta y el número son cosas distintas; aquí el bot ES la clave del índice.
   - **El alta reclama la conexión (inserta y confirma) ANTES de llamar a `setWebhook`.** Si no, pisaría el webhook del bot de otro negocio antes de devolver el 409. El candado advisory por `bot_id` serializa dos altas del mismo bot.
3. **Ruteo del webhook.** `telegram_route_connection(public_id)`, SECURITY DEFINER, devuelve solo `(tenant_id, channel_connection_id)` de una conexión no borrada. Como Twilio, no filtra `active`: el webhook lo comprueba ya dentro del negocio.
4. **Webhook `POST /webhooks/telegram/{public_id}`.** Principal anónimo; se autentica con `public_id` más el secreto por conexión (al menos 32 caracteres, aleatorio), comparado con `hmac.compare_digest`. El negocio sale solo de la ruta. Respuestas:

   | Caso | Respuesta |
   |---|---|
   | Aceptado o ignorado (grupos, `edited_message`, stickers, JSON raro) | 200 |
   | Secreto malo | 401 (auditado) |
   | `public_id` desconocido | 404 |
   | Cuerpo de más de 64 KiB | 413 |
   | Redis caído o kill-switch apagado | 503 (Telegram reintenta) |

   `setWebhook` pide solo `allowed_updates=["message","callback_query"]`.
5. **Ids globales (ADR-0007).** La llave de `ProcessedEvent` y de `messages` no lleva proveedor, y el `message_id` de Telegram es por chat. Por eso:
   - entrantes: `tg:<bot_id>:in:<chat_id>:<message_id>`; un toque de botón, `tg:<bot_id>:cb:<callback_query.id>`. No
     `update_id`: tras una semana sin updates Telegram lo elige al azar y podría repetir uno ya visto, que la
     idempotencia descartaría en silencio (cambio del PR core 3, tras la auditoría);
   - salientes: `tg:<bot_id>:out:<chat_id>:<message_id>`;
   - auditoría: `app_id="telegram:<bot_id>"`.

6. **Errores (`agentcore_channel_telegram.errors`).** Se clasifican por estado HTTP y `description`, porque `error_code` «puede cambiar»:

   | Error | Categoría |
   |---|---|
   | 401 | `AUTH`: la salida pasa la conexión a `revoked` |
   | 403 (bloqueado, desactivado, nunca escribió) | `RECIPIENT`, **nunca `OPT_OUT`**: el bloqueo es reversible y no es una baja de marketing |
   | 400 de destinatario (`chat not found`, `user not found`, `PEER_ID_INVALID`…) | `RECIPIENT` |
   | Otro 400 | `CONTENT` |
   | 404 | `CONFIG`, **nunca `AUTH`**: un `file_path` vencido no revoca una conexión sana. El token mal formado (404 en `getMe`) lo traduce el alta a 422 `telegram_token_invalido` |
   | 409 | `CONFIG` |
   | 429 | `RATE_LIMIT` con `parameters.retry_after` |
   | 5xx | `TRANSIENT` |
   | Corte de red en un envío | `AMBIGUOUS`, con un reintento: la misma política de `decide_retry` que Twilio |
7. **Botones y listas.** `CAPACIDADES_POR_TIPO["telegram"] = CapacidadesCanal(botones=True, lista=True)`, sin ventana ni plantillas.
   - Botones y lista salen como teclado inline, una fila por opción.
   - **La lista no se degrada en el adaptador** (adenda §9 del ADR-0186): degradar se decide antes de encolar, en `send_interactive_to_conversation`.
   - `callback_data` ≤ 64 bytes; si el id no cabe, va un token corto en Redis (TTL 7 d).
   - `answerCallbackQuery` se manda después de publicar, best-effort con 2 s de timeout, y `editMessageReplyMarkup` quita el teclado.
8. **Contacto.** `InboundMessage` suma UN campo aditivo, `shared_phone: str | None = None`. El adaptador lo llena solo cuando `contact.user_id == from.id`. El consumer lo guarda en `contact_phone` (E.164) solo si está vacío o es igual: no pisa otro número ni fusiona clientes. Es el contrato de canal de ADR-0052, no `packages/contracts`.
9. **Kill-switch.** `TELEGRAM_HABILITADO` (por defecto `true`) se lee solo en `core/gateway/telegram/habilitado.py`. Apagado:
   - el tipo no está disponible: `disponible_para` lo mira ANTES de `available`;
   - las rutas responden 404 y el webhook 503;
   - la salida no envía y el cron de salud no corre.
10. **Visibilidad.** El tipo entra con `available=False` y `en_selector=False`, sin `bandera_negocio`. Pasa a `True` cuando entren y salgan mensajes (PR core 4), no con el alta: un bot conectado sin entrada ni salida no atiende.
11. **Secretos fuera de la observabilidad.** El token va en la ruta (`/bot<token>/método`). Por eso:
    - el cliente valida su forma, lo enmascara (`…:ABCD`) y relanza los errores de `httpx` `from None`;
    - las trazas tapan `/bot<token>/` y `/file/bot<token>/` solo en `api.telegram.org` (`core/observability/tracing.py`);
    - Sentry tapa el patrón `<id>:<secreto>` y las claves `bot_token` y `webhook_secret`;
    - el logger `httpx` sigue en WARNING.
12. **Salud.** `core.telegram_health_check` cada 15 min con `getMe` y `getWebhookInfo`.
    - Pasa a `degraded` si hubo un error de entrega en los últimos 30 min, `pending_update_count` > 50 o la URL cambió.
    - Un 401 la pasa a `revoked`.
    - «Reparar» re-registra el webhook.

13. **La voz no está atada a una conexión** (pedido del dueño, 2026-11-16; enmienda ADR-0197 punto 6). `CapacidadesCanal` suma `voz`, declarada por tipo de conexión en `CAPACIDADES_POR_TIPO`: hoy `whatsapp_waweb` y `telegram`, los dos con `send_audio`. La política de `pack-voice` elige voz en cualquier conversación cuya conexión viva tenga esa capacidad, sin mirar el proveedor. Si un adaptador no sabe mandar audio (`ChannelError`) o el proveedor lo rechaza (`CONTENT`), `outbound_send` manda el texto ya escrito, como cuando el TTS no puede. Sumar voz a otro canal es implementar su `send_audio` y encender `voz` en su tipo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Un bot de solvershub compartido con enlaces profundos por negocio | El cliente le habla a «SolversHub», no al negocio; un solo bot reparte el ritmo de Telegram entre todos; un bot que mezcla negocios debilita el aislamiento |
| Telegram Business (el bot atiende la cuenta personal del dueño) | Exige Telegram Premium al dueño, la API es más reciente y limitada y el chat mezcla lo personal con el negocio |
| Bandera por negocio como Twilio | El dueño la descartó (D-74): Telegram no tiene costo ni aprobación de terceros; basta un kill-switch global |
| `telegram_bot_en_otro_negocio` SECURITY DEFINER, como en Twilio | Redundante con el índice único (el bot es la clave) y suma un oráculo que salta la RLS |
| Columna `bot_id` en `telegram_connections` | Duplica `external_id` sin un lector que lo necesite |
| Degradar la lista a texto numerado en el adaptador | La adenda §9 del ADR-0186 lo prohíbe: el outbox no sabe si quien encoló aceptó `sin_degradar` |

## Consecuencias

**Positivas:**
- Un canal más sin costo por mensaje y sin depender de Meta.
- Reutiliza el patrón de Twilio: alta, webhook por `public_id`, salud y rutas admin.
- Reutiliza el BFF y la ficha del panel.

**Negativas / lo que aceptamos pagar:**
- El dueño hace un paso en BotFather.
- Sin `my_chat_member`, un bloqueo solo se nota al fallar un envío.
- Texto plano en v1: el Markdown del agente se ve con símbolos.
- Los grupos se ignoran.
- Si el backend cae más de 24 h, Telegram descarta los updates pendientes.
- `AMBIGUOUS` con un reintento puede duplicar un mensaje, igual que con Twilio.
- Un token filtrado solo se rota a mano en BotFather.

**Qué haría que revisáramos esto:**
- Que un negocio pida un bot para varias sucursales o atender grupos.
- Que la Bot API agregue idempotencia en `sendMessage`.
- Que el patrón `<id>:<secreto>` aparezca en Tempo, Sentry o Kibana pese a las redacciones.
- Que más del 5 % de los envíos de un mes salgan `AMBIGUOUS`.

## Impacto en el código

- **PR core 1 (este):**
  - migración 0137 y `TelegramConnection`;
  - `packages/channel-telegram` (`client.py`, `errors.py`, `limites.py`) con el entry point `agent_core.channels`; solo expone `PROVIDER` hasta el PR core 3;
  - tipo `telegram` y su fila en `CAPACIDADES_POR_TIPO`;
  - `canales_con` ignora los tipos ocultos para todos;
  - `TELEGRAM_HABILITADO` y `core/gateway/telegram/habilitado.py`;
  - redacción en `tracing.py` y `sentry_setup.py`.
- **PR core 2:** `core/gateway/telegram/alta.py` y `operacion.py`, `core/api/routers/telegram_connections.py`, el cron de salud `core.telegram_health_check` con la migración 0138 (`admin_telegram_connection_ids()`, SECURITY DEFINER como `admin_twilio_connection_ids`), y `docs/openapi.json`. El mensaje de prueba va a un `chat_id` que ya le escribió al bot: Telegram no deja que un bot inicie la conversación.
- **PR core 3:** `core/gateway/telegram/webhook.py` (montado en `core/main.py`), `agentcore_channel_telegram.adapters.TelegramInbound`, ids globales, auditoría `telegram:<bot_id>`, `answerCallbackQuery` del toque, consumer fail-closed sin conexión y `avisos_canal` (`telegram` en `TIPOS_DE_TEXTO`, sin ventana). Los medios entran como `file_id`; la ingesta los descarga en el PR core 5.
- **PR core 4:** `TelegramOutbound` (texto en partes de 4096, foto, `sendVoice` por URL y teclado inline una fila por opción), `resolve_credentials` (solo su bot, sin respaldo, nada con el kill-switch apagado), `core/gateway/telegram/interactivos.py` (ids de más de 64 bytes como `t:<token>` en Redis 7 días, resueltos por el webhook, que además quita el teclado), `core/gateway/telegram/ritmo.py` (25/s por bot, 1/s por chat, pausa tras un 429), `outbound_send` (401 → `revoked`, 409 → `error`, voz rechazada → texto), la capacidad `voz` (§13) y `available`/`en_selector` encendidos.
- **PR core 5:** `media/ingesta.py`, `request_contact` y `shared_phone`.
- **Panel y APK:** HU-CONN-27 y HU-CONN-28.
- **Fuera del repo:** agregar el patrón del token de Telegram a la redacción de logs de Alloy (`solvershub-infra`).
