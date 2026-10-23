## Notas de voz e imágenes entrantes (HU-MEDIA-01/02/03/04/06/09)

- **ADR-0151:** la media se procesa en el consumer, no en el webhook. El webhook solo verifica,
  aplica la cuota de audio/OCR y encola; el consumer descarga de Meta, guarda con el controlador
  único, transcribe/describe y recién después persiste el inbound.
- **Descarga y storage (HU-MEDIA-02):** `core/media/meta_media.py` valida hosts de Meta, tope de
  16 MB, timeout y hash; `core/media/ingesta.py` guarda la `key` en `messages.media_url` y usa
  caché Redis por `external_message_id` para no repetir la llamada pagada.
- **Audio (HU-MEDIA-03):** transcripción con Groq/OpenAI, idioma del negocio, `[Nota de voz] …`;
  si el audio es ilegible se responde el texto configurado y no se gasta LLM.
- **Visión (HU-MEDIA-04/06):** descripción con DeepSeek/Anthropic/OpenAI, `[Foto] …`, modo
  `describir` o `sin_descripcion` por negocio y respuesta fija si no se identifica nada.
- **Configuración (HU-MEDIA-09):** migración `0103` (`app_settings.audio_*`/`vision_*`), endpoints
  `GET/PUT /admin/settings/media*`, prueba de conexión con muestras mínimas y pestaña **Multimedia**
  del panel nuevo. Las llaves se cifran y nunca se devuelven; sin llave propia se usa la de
  plataforma.
- **Costos:** cada llamada deja una fila `usage_ledger` `kind='stt'|'vision'` en modo sombra
  (`credits=0`); el cobro actual por fee de `messages` no cambia.
- **Fuera de alcance:** OCR/conciliación de comprobantes, WhatsApp Web, chat web con adjuntos, TTS
  y retención automática (documentados en SPEC-04 y ADR-0151).
