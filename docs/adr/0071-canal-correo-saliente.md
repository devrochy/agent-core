# ADR-0071 — Canal de correo saliente (sin inbound)

- **Estado:** aceptado
- **Fecha:** 2026-09-26

## Contexto

Se quiere que el asistente **envíe correos** (p. ej. la confirmación de una cita)
desde una conexión de correo del tenant. No hay recepción. El pipeline de salida
del core (`outbound_send`) modela todo envío como un `Message` y resuelve cliente y
credenciales **desde la conversación** (`Message.conversation_id` es NOT NULL).
Crear un modelo de envío paralelo (una tabla `email_messages`) duplicaría la cola,
los reintentos y la DLQ.

## Decisión

El correo es un **canal con conversaciones salientes**, reutilizando el pipeline
existente:

- **Adaptador saliente built-in** `smtp` en `core/gateway/channels/email.py`
  (como `meta`), usando `smtplib` de la stdlib en un hilo. Los canales de terceros
  siguen entrando por entry_points (ADR-0052); email es genérico y su tipo ya vive
  en el catálogo del core (ADR-0069), por eso es built-in y no un paquete aparte.
- **Destinatario** = un `Customer` (`channel="email"`, `external_id`=email) con una
  `Conversation` ligada a la conexión `email_smtp`. Aparecen en el panel de
  conversaciones como cualquier otro canal.
- **Tool `send_email`** (skill del core, `requires_connection_types=["email_smtp"]`,
  ADR-0072): busca/crea cliente y conversación, persiste el `Message` y encola
  `outbound_send`. Solo se ofrece si hay una conexión de correo activa.
- **Asunto**: `Message` no tiene columna de asunto; viaja como primera línea
  `Subject: …` del contenido y el adaptador la separa del cuerpo.
- **Sin inbound**: no hay webhook ni adaptador de entrada de correo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tabla `email_messages` paralela | Duplica cola/reintentos/DLQ y rompe "derivar, no contar aparte" |
| `Message.conversation_id` nullable | Cambio de core para un caso que se resuelve modelando la conversación |
| Paquete `packages/channel-email` | Un canal genérico cuyo tipo ya está en el core; agrega dependencia/instalación sin beneficio |
| API externa (Resend/SendGrid) | Dependencia y proveedor; SMTP cubre el caso y el tenant ya tiene su servidor |
| Columna `subject` en `Message` | Cambio de esquema para un solo canal; el prefijo del contenido es reversible |

## Consecuencias

**Positivas:** envío de correo con la misma fiabilidad que WhatsApp (cola, reintentos,
DLQ, auditoría en `messages`); cero modelo de datos nuevo; el gating por conexión ya
lo cubre ADR-0072.

**Negativas / lo que aceptamos pagar:** los correos aparecen como conversaciones (una
por destinatario); el asunto va embebido en el contenido; sin inbound no hay hilo real
de respuestas.

**Qué haría que revisáramos esto:** que se necesite correo entrante (→ adaptador
inbound + threading) o adjuntos (→ columna/almacenamiento de medios en `Message`).

## Impacto en el código

- `core/gateway/channels/email.py` (nuevo) + registro en `core/gateway/channels/registry.py`.
- `core/tools/core_skill.py` (tool `send_email`).
- Reusa `outbound_send` y `Message`/`Customer`/`Conversation` del core.
- Sin cambios en `packages/contracts`.
