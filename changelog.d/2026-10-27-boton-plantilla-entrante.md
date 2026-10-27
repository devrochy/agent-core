## WhatsApp: los toques en botones de plantilla ya no se pierden (2026-10-27)

- Canal Meta (`core/gateway/channels/whatsapp/inbound.py`). Cuando el cliente toca un botón de
  respuesta rápida de una plantilla de Meta, el webhook llega como `type: "button"` (no `interactive`).
  **Cambio de comportamiento:** antes esos toques se descartaban sin rastro; ahora se parsean igual
  que `button_reply`/`list_reply`: `content` = `button.text` e `interactive_id` = `button.payload`
  (recortado a 256).
- Efecto: un quick reply cuyo payload no tenga un prefijo conocido por el motor llega al LLM como
  texto del cliente (el `text` del botón), como si lo hubiera escrito. El resto del flujo
  (webhook → stream de Redis → consumer) no cambia.
