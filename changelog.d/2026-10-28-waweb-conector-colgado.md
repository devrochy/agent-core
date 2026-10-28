## QR de WhatsApp Web: conector colgado, tmpfs sin permisos y sesión duplicada (2026-10-28)

- El conector ya no queda mudo para siempre: si un tenant pasa 5 min sin publicar QR ni cambiar de
  estado, se alerta y se reinicia el proceso. Neonize puede dejar una llamada al puente Go trabada y
  `disconnect()` esperaba el lock sin fin; cancelar la tarea no recupera el puente.
- `disconnect()` acota solo la espera del lock (no cancela la llamada en curso, que provocaría
  `concurrent map writes` en Neonize).
- El `tmpfs` del connector debe montarse con `mode=1777`. Si la raíz de sesiones no es escribible, se
  usa una raíz efímera en `/tmp` y se registra `waweb_session_dir_no_escribible`; antes el vínculo
  fallaba con `PermissionError` y el panel se quedaba en "esperando el QR".
- `set_status` escribe en la sesión de la conexión activa: con una sesión huérfana el panel leía una
  fila y el connector actualizaba otra. La migración `0118` limpia duplicados y crea un índice único
  parcial para impedir que vuelva a haber dos conexiones `waweb` vivas por negocio.
- El QR se conserva 240 s en Redis para cubrir el vencimiento (90 s) más la reconexión.
