## WhatsApp Web: la nota de voz del agente se reproduce (2026-11-15)

- ADR-0197: neonize 0.10 arma el `audioMessage` con el mimetype de libmagic (`audio/ogg`) y WhatsApp solo reproduce una nota de voz (PTT) con `audio/ogg; codecs=opus`: el cliente recibía una burbuja vacía. `NeonizeSocket.send_audio` construye el mensaje con `build_audio_message`, fija `MIMETYPE_NOTA_DE_VOZ` y lo envía con `send_message`.
