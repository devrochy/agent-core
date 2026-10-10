## Reset de WhatsApp (QR) con la sesión vinculada (2026-10-10)

- Re-vincular desde el panel ya funciona aunque el connector tuviera la sesión conectada: antes el
  manager volvía a guardar las credenciales viejas al cerrar el socket (pisando el reset) y el
  siguiente ciclo no limpiaba porque el estado leído al arrancar la tarea seguía en `connected`. Ahora,
  con un reset pendiente, no se persiste el directorio de trabajo, y el ciclo siguiente borra el blob
  y el `session.db` local antes de reconectar, así se emite un QR nuevo.
