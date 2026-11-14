## Pruebas del panel efímeras (2026-11-14)

- HU-ARCH-14. El playground no ejecuta tools con efectos en el canal `test`: comercio, agenda,
  cobranza, suscripciones, pagos, correo y plantillas usan simuladores y estado sandbox en Redis.
- Contadores, Dashboard, Costos, fiabilidad, cuotas y listas de packs excluyen las conversaciones
  de prueba; un pedido simulado no toca `crm_order_counters` ni descuenta stock.
- Nuevo `POST /admin/playground/close` y purga física acotada (`purge_test_conversation`): al
  cerrar o reiniciar la prueba no quedan conversación, cliente, mensajes, tareas, ledger,
  decisiones, auditoría ni memoria. Tarea horaria para pruebas huérfanas.
- Auditoría de CI: toda tool declara `solo_lectura` o `side_effects` + simulador.
- Panel y APK cierran la prueba al salir y muestran «Acción simulada». ADR-0194.
