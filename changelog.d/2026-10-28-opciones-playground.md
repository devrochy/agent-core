## Opciones directas: probar una plantilla con botones en el playground, sin Meta (2026-10-28)

- SPEC-40 / ADR-0172, pista B1. El canal `test` (playground) cuenta como canal con botones: `decidir_via` lo
  manda por la vía interactiva (o texto si no cabe en los topes) sin ventana de 24 h ni plantilla, y
  `send_to_conversation` / `send_interactive_to_conversation` guardan el mensaje como entregado, sin Celery ni
  `ON_MESSAGE_SENT` (mismo patrón que `playground._guardar_mensaje`). `enviar_confirmacion` del toque también
  acepta `test`, para que `optc`/`optn` funcionen. Los demás canales no cambian.
- Rutas nuevas: `POST /api/v1/admin/playground/options` (`{clave}`; envía la versión vigente a la conversación de
  prueba) y `POST /api/v1/admin/playground/tap` (`{payload}`; mismo resolver y misma firma que WhatsApp).
  Devuelven los mensajes producidos para que el panel los pinte. Dueño o master con escritura en `connections` y
  bandera `opciones_directas.habilitada` encendida (404 si no). **La tool se ejecuta de verdad** contra los datos
  del negocio.
- Sin migración.
