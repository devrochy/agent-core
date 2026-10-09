## Un cliente no puede tocar la cotización ni el pedido de otro (2026-10-08)

- Seguridad (IDOR dentro del tenant, auditor-multitenant en HU-SUB-07). `confirmar_cotizacion`, `crear_pedido` y
  `aplicar_promocion` buscaban la cotización solo por `quote_id` + `tenant_id`, y `agregar_item_pedido` y
  `confirmar_pago_pedido` el pedido solo por `order_id` + `tenant_id`: un cliente que conociera el UUID de otro
  cliente del mismo negocio podía confirmarla, aplicarle una promoción, convertirla en pedido o sacar el link de pago.
  Ahora también filtran por el `customer_id` de la conversación; la ajena responde "no encontrada", igual que una
  inexistente. Tests en `tests/integration/test_pack_commerce_idor_cliente.py`.
