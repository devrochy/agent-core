## Contratar un plan y tomar un pedido se completan cuando el pago se confirma (2026-10-11)

- Decisión del dueño: con pago en línea, `pack_commerce.contratar_plan` y `pack_commerce.tomar_pedido` (v1.1.0, pack-commerce 2.2.0) ya no terminan al mandar el link, sino cuando se confirma el pago. Esperan en la etapa nueva `esperar_pago`. Con transferencia, contraentrega o plan Free terminan al crear el pedido, como antes.
- Cuando su handler marca un pedido como pagado (con el monto ya validado), pack-commerce escribe el evento `order.paid` en el outbox. Ese evento cierra la tarea cuyo `order_id` coincide, sin cambiar lo que ve el modelo.
- Tool nueva `verificar_pago_pedido`, de solo lectura y disponible solo con el motor activo. Sirve para "ya pagué" e informa si el pago está pendiente o fue rechazado. En la espera, `confirmar_pago_pedido` reenvía el link o genera uno nuevo si el anterior venció.
- Un pago rechazado no cierra la tarea: el cliente puede reintentar. Un cron horario (`core.kernel_esperas_vencidas`) cancela con el motivo `espera_vencida` las esperas sin actividad de más de 72 h. Solo recorre negocios con `aos_procedures`.
- Cambios en el motor:
  - al salir de la última etapa, la tarea se completa aunque declare eventos de cierre;
  - después de un efecto en una etapa de riesgo alto, los datos quedan congelados por el resto de la tarea;
  - el cierre por evento busca las tareas por el valor de la clave, sin el tope de 200 abiertas.
- ADR-0113: la adenda HU-AOS-07 queda aceptada por el dueño, con la nueva sección "Cierre". Se actualiza la adenda HU-AOS-05.
