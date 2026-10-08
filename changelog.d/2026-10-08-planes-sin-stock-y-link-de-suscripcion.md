## El asistente puede vender planes y dar el link de una suscripción (2026-10-08)

- Un plan recurrente (`billing_period`) ya no se controla por inventario: con stock 0 (como el catálogo de
  `solvershub`), `consultar_inventario` lo marcaba no disponible y `generar_cotizacion` lo rechazaba ("No hay stock
  suficiente"), así que el asistente escalaba en vez de vender. Ahora figura disponible (`stock: null`), se cotiza y
  el pedido no descuenta inventario.
- El guardrail `crm_completion_check` acepta el link de `sub_link_pago` (período de una suscripción) y de
  `generar_link_pago` como evidencia de "link de pago"; antes solo el del pedido, y el aviso del cargo terminaba en
  escalamiento.
- Ambos los encontraron los journeys de HU-SUB-12.
