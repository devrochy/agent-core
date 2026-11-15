## WhatsApp Web (QR) ya entiende audios e imágenes (2026-11-14)

- ADR-0196. El canal `waweb` descarga notas de voz y fotos con Neonize, las transporta por Redis y el consumer las procesa con el mismo STT/visión de Meta y Twilio. La cuota de audio/OCR se aplica antes de encolar. Un fallo de descarga responde el texto fijo de media no recibida, nunca en silencio.
- La API de conversaciones expone `media_type`, `media_url` firmada y `media_transcription`; el panel muestra el reproductor de audio, la transcripción y la imagen. Las imágenes salientes por QR ahora salen nativas, no como una URL en el texto.
