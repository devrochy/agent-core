# HU-004 · Conversar con ejecución de tools

**Como** cliente, **quiero** escribir por WhatsApp y que el asistente resuelva con
datos reales (inventario, citas, leads), **para** no esperar a un humano.

## Criterios de aceptación

- [ ] El webhook responde 200 en <1s (el LLM nunca corre en el request).
- [ ] Mensajes duplicados de Meta se descartan (Redis NX 48h) sin doble respuesta.
- [ ] Si escribo 3 mensajes seguidos, recibo UNA respuesta con todo el contexto
      (debounce; el timer se reinicia con cada mensaje).
- [ ] El agente usa tools cuando corresponde; los IDs internos (conversation_id,
      idempotency_key) jamás son visibles ni manipulables por el LLM.
- [ ] Respuestas largas llegan como burbujas separadas (700ms entre cada una).
- [ ] Datos exactos (links, montos de tools) llegan intactos (SafeRenderer).
- [ ] Si una tool falla, el asistente se disculpa y sigue funcionando (el plugin
      queda desactivado y visible en el panel) — el turno NO se pierde.
- [ ] Máximo 6 iteraciones de loop; si se agotan, escala a humano.

## Flujo técnico

`gateway/webhooks.py` → stream → `orchestrator/consumer.py` → `debounce.py` →
`loop.py` (memoria + skill router + prompt builder + difficulty router + guardrails)
→ `sender.py` (splitter + burbujas).
