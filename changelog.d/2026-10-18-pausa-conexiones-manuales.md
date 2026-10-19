## Pausar una conexión manual o de WhatsApp Web la detiene (2026-10-19)

- Corrección de seguridad (HU-AOS-23, #472; adenda 2026-10-19 de ADR-0122). Una conexión sin cuenta de Meta
  (manual, WhatsApp Web, correo, chat web) desactivada o eliminada ya no responde: lo que entra se guarda sin correr
  la IA (`blocked_by = connection_paused`) y nada sale por ella (respuestas en vuelo, crons de packs, avisos por
  evento, operador). Push «Conexión pausada» a los dueños, una vez al día, con `scope` en el payload.
- La consola de conexiones marca como `pausable` también las conexiones manuales y de WhatsApp Web.
