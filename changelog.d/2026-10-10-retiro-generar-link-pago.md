## Se retira `generar_link_pago`: el modelo ya no elige referencia ni monto de un cobro (2026-10-10)

- Hallazgo de `auditor-multitenant` (severidad media, no cruza tenants): la tool recibía `reference` y
  `amount` del LLM y buscaba las transacciones solo por negocio. Un cliente que conociera el número de
  pedido de otro (`OC-0002`) sabía si estaba pagado y por cuánto, y podía crear un link con esa referencia
  y un monto inventado, que después bloqueaba el link del dueño del pedido.
- pack-payments ya no le ofrece tools al modelo. Sigue aportando la capacidad de pagos, el webhook y la
  pestaña de configuración. `pack_payments/skills.py` pasa a `pack_payments/proveedor.py`.
- El flujo de commerce ("Pedido y cierre") ofrece `confirmar_pago_pedido`, que toma el pedido del cliente
  de la conversación y el monto del servidor (ADR-0087). El playbook `pagos` ya no nombra tools ni le pide
  el monto al modelo.
- Test de integración `aislamiento`: por número de pedido, un cliente no ve el estado ni el monto del
  pedido de otro (`consultar_estado_pedido` responde igual que con un número inexistente).
- Cambia la lista de tools del modelo: hay que regrabar el gate de evals.
- Adenda en ADR-0087.
