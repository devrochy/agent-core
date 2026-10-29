## WhatsApp Web (QR): el conector se reinicia solo cuando se agota su pool de hilos (2026-10-29)

- **Síntoma:** el agente "respondía" en el panel pero al cliente no le llegaba nada: los mensajes quedaban en
  `queued` y el worker fallaba cada envío con «WhatsApp (waweb) no está conectado para este negocio».
  Los mensajes entrantes seguían llegando. Se recuperaba solo reiniciando el conector, y volvía a pasar
  unos 35–45 min después.
- **Causa:** en neonize 0.5.2 cada ciclo de QR de una sesión sin vincular deja un hilo del executor por
  defecto bloqueado dentro de `Neonize()` (Go); `disconnect()`, `stop()` y cancelar `connect_task` no lo
  liberan. Con `min(32, CPU + 4)` hilos (12 en test) el pool se agotaba en unos 9–12 ciclos; entonces
  ninguna llamada al puente arrancaba, el drain del negocio dejaba de refrescar `waweb:ready` (TTL 120 s)
  y la API rechazaba cada envío. Ningún watchdog lo veía: la sesión seguía `connected` en la base y la
  sesión que cicla sí "progresaba".
- **Cambio:** `WaWebConnector._check_executor` lanza un no-op en el executor en cada barrido; si no arranca
  en 10 s dos veces seguidas, alerta (`critical`) y reinicia el proceso (Docker lo levanta y las sesiones
  vinculadas se restauran del blob guardado). El executor por defecto del conector pasa a 64 hilos para
  espaciar esos reinicios. No se arregla la fuga (es de neonize): ver el seguimiento en el PR.
