# ADR-0197 — Respuestas por voz con `pack-voice`

- **Estado:** aceptado; el punto 6 («solo `waweb`») está enmendado por ADR-0199 §13: la política decide por la capacidad `voz` del tipo de conexión
- **Fecha:** 2026-11-14
- **Usa:** ADR-0196 (media en `waweb`), ADR-0085 enmendado (controlador único de almacenamiento), ADR-0103 (anti-baneo del canal QR), ADR-0190 (habilidades por agente)

## Contexto

El agente responde siempre en texto. Un negocio que atiende por WhatsApp Web (QR) quiere
que, cuando el cliente manda una nota de voz, algunas respuestas también salgan como nota
de voz, sin convertir todos los turnos en audio: una mezcla de texto y voz, configurable,
que no sorprenda al cliente ni dispare el riesgo del canal no oficial.

La síntesis no puede correr en el turno: agregaría segundos de latencia y acoplaría el
core a un proveedor de TTS. Tampoco puede ser una tool que el modelo decida: la respuesta
final todavía no existe cuando la tool corre.

## Decisión

1. **Dos extensiones genéricas en el core.** `MCPHost` registra una *política*
   (`VoicePolicyFn`) y un *sintetizador* (`VoiceSynthesizerFn`) por pack. El egress
   consulta la política antes de entregar y, si pide voz, encola el turno con
   `send_voice_to_conversation`; el sintetizador corre después en el worker de salida.
2. **El pack decide la mezcla.** `pack-voice` es transversal y configurable por negocio:
   `modo` (`mixto`/`siempre`/`nunca`), `solo_si_cliente_manda_audio`,
   `probabilidad_voz` (0-100), `evitar_voz_seguida`, `incluir_texto` y `max_caracteres`.
   Con los valores por defecto (`mixto`, 50 %, solo si el cliente mandó audio, sin dos
   voces seguidas, voz + texto, tope 600 caracteres), un audio recibe a veces texto y a
   veces texto + voz, nunca dos voces consecutivas.
3. **La respuesta con imagen o confirmación no se convierte.** El egress solo consulta la
   política para texto sin imagen y sin confirmación de procedimiento.
4. **Texto primero.** Si `incluir_texto` está activo, el texto se encola por el camino
   normal; después la fila `audio/tts`. Si el TTS falla en el worker, se envía el texto
   (o ya estaba enviado): la respuesta nunca se pierde.
5. **Audio por el storage, no por el stream.** El worker guarda el OGG/Opus con el
   controlador único, deja una URL firmada en `messages.media_url` y el adaptador de
   `waweb` la manda como `PTT=True` por Neonize. El texto de la nota vive en
   `media_transcription` para el panel.
6. **Solo `waweb` en esta tanda.** El adaptador de canal declara `send_audio`; Meta y
   Twilio lo dejan sin implementar y la política no los elige.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Sintetizar en el turno | Suma latencia y acopla el turno al proveedor de TTS |
| Tool que envía el audio y silencia el texto | El modelo tendría que decidir con una respuesta que aún no existe; silenciar el egress es frágil |
| Un guardrail que reescriba el texto | Los guardrails solo aprueban o reintentan, no transforman la entrega |
| Solo voz (sin texto) | El negocio pidió explícitamente voz + texto y un fallback legible |
| Voz en todos los audios | El cliente pidió una mezcla, no saturar el canal |

## Consecuencias

**Positivas:** el negocio regula la mezcla sin tocar código; el core solo gana dos hooks
genéricos; el TTS corre en el worker y respeta reintentos/DLQ; el texto es el fallback
natural.

**Negativas / lo que aceptamos pagar:** una consulta más por turno cuando el pack está
activo; el costo del TTS (llave de plataforma o BYOK) y el riesgo de voz sintética en un
canal no oficial; la URL firmada tiene el TTL del canal (1 h) y debe ser alcanzable desde
el connector.

## Impacto en el código

`core/mcp/types.py`, `core/mcp/host.py`, `core/kernel/egress.py`,
`core/gateway/sender.py`, `core/tasks/outbound_send.py`,
`core/gateway/channels/base.py`, `core/waweb/{handoff,outbound,backend,backend_neonize}.py`,
`packages/channel-waweb`, `packages/pack-voice/**` (nuevo), `core/config.py`
(`TTS_TIMEOUT_S`). Sin migración y sin cambio de contrato congelado.
