## Superpoder «Pago en línea» para pedidos existentes (2026-11-17, HU-COM-23)

- Procedimiento `pack_commerce.pago_en_linea` (dominio «Formas de pago»): `elegir_pedido` → `enviar_link` → `esperar_pago` → `cerrar`. Disparadores de pedidos que ya existen («ya pagué», «no me llegó el link», «el link venció», «estado de mi pago»); nunca «quiero pagar». Reusa `consultar_estado_pedido`, `confirmar_pago_pedido` y `verificar_pago_pedido`; afirmaciones de link y de pago confirmado con dato del servidor.
- `consultar_estado_pedido` devuelve `order_id` (el mismo de Referencias vigentes) y el estado de pago en palabras (`en revisión`, `pago rechazado`).
- No declara `requires: pack_payments` (no trae acciones; el linter lo rechaza): sin proveedor la forma queda «Falta configurar» (HU-COM-21) y `confirmar_pago_pedido` responde que no está disponible.
- Golden de ruteo: catálogo `tienda_pagos` (12 casos) sin solapamiento con `tomar_pedido`; con `tomar_pedido` en foco, «ya pagué» no abre otra tarea.
