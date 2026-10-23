# ADR-0151 — Media entrante (notas de voz e imágenes) procesada en el consumer

- **Estado:** aceptado (HU-MEDIA-01/02, SPEC-04). Decisiones del dueño del 2026-10-22: STT con **Groq**, visión con **DeepSeek**, retención diferida y WhatsApp Web en una tanda posterior.
- **Fecha:** 2026-10-22
- **Contexto:** SPEC-04 «Notas de voz y comprobantes» en `solvershub-docs`. **Usa:** ADR-0008 (usage ledger), ADR-0014 (playbooks), ADR-0026 (override por tenant), ADR-0045/0144 (cuotas y crédito), ADR-0085 enmendado (controlador único de almacenamiento), ADR-0115 (conexión canónica de Meta). **Deja fuera:** OCR/conciliación de comprobantes (HU-MEDIA-05), WhatsApp Web, chat web con adjuntos, TTS y visor de media.

## Contexto

Hoy el webhook de WhatsApp tolera un audio o una imagen, pero no la procesa: `media_url` queda vacío y `mod_media_processor` no existe. El cliente que manda una nota de voz o una foto recibe una respuesta genérica o ninguna. `agent-sale` ya resolvió el flujo (descarga, STT, visión y caché de idempotencia), pero en un solo tenant, con base64 en Postgres y prompts en código.

El webhook debe responder a Meta en pocos segundos. Descargar y transcribir dentro de ese request expone el webhook a timeouts y reintentos de Meta, así que el procesamiento no puede vivir ahí.

## Decisión

1. **Procesar en el consumer, antes del turno conversacional.** El webhook sigue verificando la firma, reclamando idempotencia (`claim_message_once`), revisando la cuota de audio/OCR y encolando. El consumer descarga, guarda, transcribe/describe y recién después persiste el inbound y agenda el debounce o el turno.
2. **Guardar con el controlador único, no en base64.** `core/almacenamiento.guardar(kind="media", ...)` escribe bajo `TENANTS_ROOT/<slug>/media/`. `messages.media_url` guarda la `key` relativa (no una URL firmada, que expira); `media_transcription` guarda el texto y `media_type` el MIME. No se crea `inbound_media` en esta tanda: el registro `stored_files` y la retención quedan como paso siguiente (decisión del dueño: documentar y diferir).
3. **Idempotencia en dos capas.** El `ProcessedEvent` del pipeline sigue siendo la capa principal. Encima, una clave Redis `media:result:{tenant}:{external_message_id}` (TTL 1 h) evita repetir la llamada pagada si el reintento ocurre después de la transcripción/descripción y antes de persistir. El nombre del archivo es determinístico por mensaje, así que un reintento reemplaza el mismo archivo en vez de duplicarlo.
4. **Configuración por negocio con fallback de plataforma.** `AppSettings` gana `audio_*` y `vision_*` (proveedor, modelo, llave cifrada, habilitado y modo). Si el negocio no configura, se usan `STT_*`/`VISION_*` (y las llaves por proveedor) del entorno. La llave propia se cifra con `SecretBox` y nunca se devuelve por la API; el panel solo muestra `has_api_key`.
5. **Proveedores de esta tanda:** audio OpenAI/Groq (por defecto Groq `whisper-large-v3-turbo`); visión Anthropic/OpenAI/DeepSeek (por defecto DeepSeek `deepseek-flash`, con `thinking` apagado). El idioma del STT sale de `AppSettings.language`.
6. **Imágenes:** por defecto se describen y el turno sigue como texto (`[Foto] …`). Un interruptor por negocio permite `sin_descripcion`: no se llama a visión y el agente solo acusa recibo/pregunta. La clasificación producto/comprobante y el OCR quedan para HU-MEDIA-05/06 completas.
7. **Prompts como datos.** La descripción vive en `core/playbooks/kernel/media.md`; las respuestas fijas (audio ilegible, imagen no identificada, proveedor no disponible) en `core/playbooks/respuestas.md`, con el override por tenant ya existente (ADR-0026). Nada de prompts en `.py`.
8. **Costos en modo sombra.** Cada llamada escribe una fila `usage_ledger` con `kind='stt'|'vision'`, modelo, costo real y `credits=0`; el cobro actual por fee de `messages` no cambia. Pasar el cobro al ledger es el paso siguiente de ADR-0144.
9. **Límites y seguridad.** Descarga de Meta: primero `GET /{media_id}` con el token de la conexión, luego los bytes; solo HTTPS y hosts de Meta/`fbcdn`, tope 16 MB y timeout 20 s. Un archivo que excede el tope, una URL inesperada o un audio sin texto no rompen el turno: se responde con el texto configurado y se registra.
10. **Alcance de canales.** Esta tanda procesa solo `provider="meta"` (`whatsapp`). `waweb` y el chat web siguen su camino actual; se documentan como extensión posterior.

## Consecuencias

- El turno espera la descarga + una llamada de IA (segundos). Está acotado por `TURN_TIMEOUT_S` y por los timeouts propios de media; no bloquea el webhook.
- Si el proveedor falla de forma transitoria, el consumer reintenta y la caché evita volver a pagar. Si falla de forma permanente (401/403/400), se responde el mensaje configurado y no se reintenta en vano.
- Los archivos quedan sin retención automática hasta que se implemente `stored_files` + el job de limpieza. Es una deuda explícita, no un olvido.
- El OCR de comprobantes no se activa por accidente: sin clasificación, una foto se describe como cualquier imagen; la ruta financiera sigue fuera del LLM.
- La configuración queda en el panel nuevo (pestaña Multimedia). El panel SSR clásico no se toca en esta tanda.
