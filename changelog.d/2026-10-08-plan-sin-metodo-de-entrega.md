## Un plan se pide sin método de entrega (2026-10-08)

- `crear_pedido` exigía `delivery_method` también para planes (un servicio): el asistente insistía en "¿domicilio o
  recoger en tienda?" con el Plan Free y nunca creaba el pedido, y la respuesta decía "entregado en None, None,
  None". Ahora es opcional para planes (no se pregunta ni se muestra), los productos lo siguen pidiendo, y un plan
  nunca pide dirección aunque la config del negocio sea anterior a la clave `plan` de `checkout_fields`. Encontrado
  por los journeys de HU-SUB-12.
