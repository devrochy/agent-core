## Un pedido pagado de otra compra ya no cubre la compra actual (2026-10-10)

- **Incidente en test** (tenant solvershub, 2026-10-10, 19:34–19:49 UTC):
  - La clienta había pagado el Basic (pedido `OC-0001`) y, en la misma conversación, quiso comprar el Premium. Había cotización y ningún pedido nuevo.
  - El bloque de referencias mostraba `OC-0001` como el pedido de la conversación.
  - El modelo le dijo que "el pedido OC-0001 ya figura como pagado, así que no hace falta generar otro". El Premium no estaba pagado.
- **Referencias vigentes** (adenda de ADR-0130):
  - Entre la cotización vigente y el último pedido, el más nuevo es la compra en curso.
  - La cotización sale con `pedido=ninguno`. Un pedido de otra compra, o uno cancelado, entregado o expirado, sale como `pedido_anterior`: sin total y con su `order_id`, para poder cobrarlo si está pendiente.
  - Una cotización más vieja que el último pedido ya no se muestra.
- **Playbook `ventas.md`:** `pedido_anterior` es otra compra y su pago no cubre la cotización en curso. Con `pedido=ninguno` toca seguir con `confirmar_cotizacion` y `crear_pedido`. Nunca decir "ya está pagado" ni "no hace falta pagar" por el pago de otro pedido.
- **Verificador:** la afirmación nueva `pago_registrado` cubre "figura como pagado", "el pago quedó registrado", "ya pagaste" y "recibimos tu pago". Pasa solo si en el turno se llamó `consultar_estado_pedido` o `sub_consultar`. Las negaciones y los condicionales ("si ya pagaste…", "apenas recibamos tu pago…") no cuentan como afirmación.
  - El set etiquetado suma 11 casos (2 del incidente): 218 correctas y 21 falsas.
  - 1 falso positivo (0,5 %), aceptado a propósito: "tu pago quedó registrado" sin consultar el estado en el turno.
  - 0 falsos negativos.
- **Journey** (test de integración con el loop real y un modelo guionado): la clienta compra y paga el Basic y después compra el Premium en la misma conversación.
  - Termina con dos pedidos, el del Premium pendiente.
  - Con el bloque como estaba antes, la frase del incidente la frena el verificador.
- Test nuevo: ningún playbook puede pasar el tope de 8 KB. Pasado el tope, `load_playbook` lo descarta sin avisar, y `ventas.md` ya pesa 7,5 KB.
