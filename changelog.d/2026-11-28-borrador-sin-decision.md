## La prueba de un borrador ya no intenta escribir en core_decisions (2026-11-28)

- Un turno de prueba de borrador del panel (HU-AOS-72) corre con una release efímera, sin `release_id`, y aun así
  intentaba insertar su cabecera en `core_decisions` con `outcome = 'routed'`: el CHECK `ck_core_decisions_contract`
  la rechazaba y el worker dejaba un `core_decision_write_failed` con traza por cada turno. Ahora
  `fijar_release_del_turno` marca el turno con `omitir_decision()` y `decidir` no escribe esa fila. El CHECK no se
  toca; el chat de prueba normal y los turnos reales siguen escribiendo su decisión con release. Cierra #951.
