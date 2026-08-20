# HU-005 · Escalar a un humano

**Como** cliente, **quiero** pedir un asesor y que el bot se aparte de inmediato;
**como** admin, **quiero** ver y atender esa conversación desde el panel.

## Criterios de aceptación

- [ ] Keywords configurables ("asesor", "humano", "reclamo"...) escalan
      DETERMINÍSTICAMENTE antes de llegar al LLM.
- [ ] También escalan: refusal del LLM, fallo doble de guardrail, error del proveedor
      LLM, loop agotado, y cualquier plugin vía `host.trigger_escalation()`.
- [ ] Al escalar, el bot queda pausado para ESA conversación automáticamente
      (`conversations.bot_paused = true`) — nunca responde encima del humano.
- [ ] El cliente recibe un mensaje fijo de transición (no generado por el LLM).
- [ ] /admin/escalaciones lista pendientes con razón y resumen; enlaza al hilo.
- [ ] El admin responde como humano desde el detalle (sender_type="human").
- [ ] "Marcar resuelta" reabre la conversación y reactiva el bot.
- [ ] Todo queda en `audit_log` (inmutable) y emite `escalation.triggered`.
