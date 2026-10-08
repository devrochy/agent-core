## El link del cargo de una suscripción le llega al cliente (2026-10-08)

- `sub_link_pago` devolvía la URL solo en el resultado de la tool: si el modelo no la copiaba, el cliente leía "te
  envío el link de pago" sin link. Ahora la tool trae `_safe_append` con el link exacto, el plan, el período y el
  monto, y el core lo adjunta (como el link de un pedido). Encontrado por los journeys de HU-SUB-12 (3 de 3 rondas).
