# Suscripciones (pack_subscriptions)

Estado de la suscripción de un `Customer` (el mismo cliente del core que commerce,
calendario y cobranza usan como pivote — ADR-0056).

## Estados
- `trialing`: período de prueba activo; el cargo arranca al terminar.
- `activa`: servicio activo; `proximo_cargo` indica el próximo cargo del período.
- `pausada`: servicio suspendido; no hay cargos.
- `cancelada`: servicio finalizado; `fecha_cancelacion` documenta el fin.
- `vencida`: (uso futuro) no se renovó el cargo.

## Cobro del período (Opción B)
- La **primera compra** del plan va por commerce (pedido → link). Al confirmarse el
  pago, el pack **activa** la suscripción automáticamente (idempotente por cliente).
- Los **períodos siguientes** se cobran con `sub_link_pago` → cargo `pendiente` del
  período → `payment.confirmed` lo marca `pagado` (referencia `sub-<cargo.id>`).

## Reglas
- No prometer cambios de estado sin llamar la tool (`sub_pausar_suscripcion`, etc.).
- No cancelar sin confirmación explícita del cliente.
- El valor del período sale del `Plan`; nunca inventarlo.