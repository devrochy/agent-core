## WhatsApp Web: la sesión vinculada no se pierde en reinicios (2026-10-26)

- Conector `waweb` (ADR-0074). Corrige la pérdida del emparejamiento tras reinicios/despliegues
  (test: `licoreria-010cb7` y `solvershub` quedaron en `pending_qr` con un `session.db` vacío).
  Causa confirmada con tests: `run_session` y el watchdog borraban el blob de una sesión que SÍ se
  había vinculado cuando la fila quedaba en `error`/`pending_qr` (o no reconectaba a tiempo), porque
  decidían con `status` y con memoria del proceso; el backend además dejaba la fila en `error` en cada
  `connecting`, y `persist` pisaba el blob con un directorio sin dispositivo.
- «Nunca vinculada» ahora sale de un dato durable (`SessionContext.linked_before`: `last_connected_at`/`jid`
  más credenciales guardadas). Toda limpieza registra `waweb_session_cleared` con su `motivo`.
- `persist` solo guarda un directorio restaurado del blob o de una sesión que llegó a `connected`.
- Watchdog de QR vencido (`qr_expired_s`, 90 s): recicla para emitir un QR nuevo sin tocar el blob.
- `waweb:ready` se refresca solo con el socket conectado (los envíos fallan rápido sin conexión).
- Sesión vinculada que vuelve a pedir QR: `last_error` «Sesión perdida: re-vincular por QR» + alerta.
- `docs/despliegue-coolify.md`: despliegue del conector sin solape y con `stop_grace_period`.
