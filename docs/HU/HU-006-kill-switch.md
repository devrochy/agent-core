# HU-006 · Pausar el bot (kill-switch en 3 niveles)

**Como** admin, **quiero** pausar el bot global, por cliente o por conversación,
**para** intervenir sin apagar el sistema ni perder historial.

## Criterios de aceptación

- [ ] Tres niveles independientes evaluados en orden:
      `app_settings.bot_paused` (global) → `customers.bot_paused` (cliente) →
      `conversations.bot_paused` (conversación).
- [ ] Con cualquiera activo: el mensaje entrante SE GUARDA en el historial pero
      no genera respuesta automática (ni llamada al LLM = cero costo).
- [ ] Toggle global en /admin/configuracion; por conversación en /admin/conversaciones.
- [ ] El escalamiento activa el nivel conversación automáticamente (HU-005).
- [ ] Al reanudar, el próximo mensaje del cliente procesa normal (el historial
      guardado durante la pausa entra como contexto).
