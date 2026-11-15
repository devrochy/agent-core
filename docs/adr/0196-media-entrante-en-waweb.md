# ADR-0196 — Media entrante en WhatsApp Web (audio e imagen)

- **Estado:** aceptado
- **Fecha:** 2026-11-14
- **Enmienda:** ADR-0074 (canal `waweb`), ADR-0151 (media entrante en el consumer)
- **Usa:** ADR-0045 (cuota de audio/OCR), ADR-0085 enmendado (controlador único de almacenamiento), ADR-0121 (proceso `connector`)

## Contexto

El canal `waweb` (QR, Neonize) solo publicaba texto: `NeonizeSocket` leía
`conversation`/`extendedTextMessage` y descartaba el resto. Una nota de voz llega como
`audioMessage` (con `PTT=true`) y una foto como `imageMessage`; ambas se perdían sin log ni
respuesta. El procesamiento de media del consumer (ADR-0151) excluía explícitamente a
`waweb` porque la descarga vive en el webhook de Meta o en la URL de Twilio.

El connector no comparte el volumen `tenants-data` con la API, así que no puede guardar el
archivo en el storage del negocio y esperar que el consumer lo lea. El socket sí tiene el
único acceso posible al archivo: el propio cliente de Neonize.

## Decisión

1. **El socket descarga.** `NeonizeSocket` reconoce `audioMessage` e `imageMessage`,
   valida `MEDIA_MAX_BYTES` y descarga con `client.download_any(message)` bajo
   `_BRIDGE_LOCK` (las llamadas al puente Go ya se serializan en este canal).
2. **Los bytes cruzan por Redis, no por el stream.** `publish_inbound` guarda el archivo
   en base64 bajo `waweb:media:{tenant_id}:{wamid}:{i}` (TTL 6 h) y publica en
   `agent_core:inbound` solo el descriptor `{type, redis_key, bytes}`. El stream conserva
   el mismo formato que Meta/Twilio y no se infla con binarios.
3. **El consumer procesa igual que en los otros canales.** `core/media/ingesta.py` acepta
   `provider=waweb`, lee el descriptor con `core/media/waweb_media.py` (valida prefijo del
   tenant, MIME, tamaño y base64) y reutiliza `_procesar_audio` y `_procesar_imagen`:
   STT/visión, ledger en sombra, caché de idempotencia y respuestas fijas no cambian.
4. **Cuota en el ingress.** `publish_inbound` aplica el bloqueo duro de
   `conversaciones_audio`/`conversaciones_ocr` (ADR-0045) antes de encolar, con el mismo
   criterio que el webhook. Un fallo del chequeo de cuota no descarta el mensaje: se
   registra y se deja pasar.
5. **Fallo de descarga nunca es silencio.** Si el socket no pudo descargar, el descriptor
   viaja con `error`; el consumer responde el texto fijo `media_download_failed`. Si la
   clave de Redis expiró, se responde el mismo texto.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Base64 dentro del stream | Infla el stream y mezcla binarios con el contrato de los webhooks |
| Montar `tenants-data` en el connector y guardar en storage | Acopla el connector al volumen de la API y duplica el controlador de archivos |
| Transcribir en el connector | Duplica configuración, ledger, cuotas y respuestas fijas fuera del consumer |
| Dejar `waweb` fuera (statu quo) | El cliente que manda un audio o una foto no recibe respuesta |

## Consecuencias

**Positivas:** audios y fotos entran al mismo pipeline que Meta/Twilio; no hay cambios de
contrato congelado; el panel y el historial conservan `media_type`, `media_url` y
`media_transcription`.

**Negativas / lo que aceptamos pagar:** una descarga lenta en el connector ocupa el
`_BRIDGE_LOCK` y puede retrasar envíos de otros negocios; el TTL de Redis acota la
recuperación de un mensaje que el consumer no procesó a tiempo. La misma tanda corrige el
envío de imágenes salientes por `waweb`: antes salían como URL en el texto y ahora salen
nativas con `send_image`.

## Impacto en el código

`core/waweb/backend.py`, `core/waweb/backend_neonize.py`, `core/waweb/inbound.py`,
`core/media/waweb_media.py` (nuevo), `core/media/ingesta.py`, tests de `waweb` y `media`.
Sin migración de base de datos y sin cambio de contrato congelado.
