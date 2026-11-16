## Canal Telegram: fotos, voz y teléfono compartido (ADR-0199, PR core 5) (2026-11-16)

- Las fotos y las notas de voz que llegan por Telegram se descargan (`core/media/telegram_media.py`), y se transcriben y entienden como en WhatsApp.
  - La descarga usa `getFile` y el token del bot de la conexión de la conversación, con un tope de 20 MB.
  - Los errores salen clasificados y sin el token.
- **Teléfono compartido**:
  - `InboundMessage.shared_phone` es un campo nuevo y aditivo.
  - El adaptador lo llena solo con el contacto del propio remitente, en E.164.
  - El consumer lo guarda en `contact_phone` sin pisar otro número ni fusionar clientes.
  - El número no va en el texto del turno, que lleva «[Compartió su teléfono]».
- Sentry tapa `phone_number`, `contact`, `shared_phone` y `telefono`.
- Pedir el teléfono (`request_contact`) queda para un PR aparte, como tool del agente.
