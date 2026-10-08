## Pedir el link de un cargo ya avisado no rompe la suscripción (2026-10-08)

- El cron genera el cargo con su link (HU-SUB-05). Cuando el cliente pedía el link (`sub_link_pago`), se creaba otro
  en la pasarela con la misma referencia `sub-<cargo>` y el insert chocaba con el índice único: link huérfano en
  Wompi y una excepción que el loop trataba como falla del pack ("La herramienta no está disponible"). Ahora
  `charge_link` reusa el link del cargo y `create_link` de pagos es idempotente por referencia (mismo monto → el link
  existente; otro monto → rechazo de negocio). Encontrado por los journeys de HU-SUB-12.
