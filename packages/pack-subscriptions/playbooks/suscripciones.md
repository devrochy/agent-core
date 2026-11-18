# Suscripciones (pack_subscriptions)

Estado de la suscripción de un `Customer` (el mismo cliente del core que commerce,
calendario y cobranza usan como pivote — ADR-0056).

## Estados
- `trialing`: período de prueba activo; el cargo arranca al terminar.
- `activa`: servicio activo; `proximo_cargo` indica el próximo cargo del período.
- `pausada`: servicio suspendido; no hay cargos.
- `cancelada`: servicio finalizado; `fecha_cancelacion` documenta el fin.
- `vencida`: un cargo de período siguió impago pasados los días de gracia del negocio, o un plan Free
  terminó su prueba sin pasar a un plan pago. No genera cargos nuevos; al pagar, la suscripción vuelve a `activa`.

## Ciclo de vida
- La suscripción nace al **inscribir** al cliente en un plan de este pack
  (`sub_inscribir`). Con plan de pago queda `pendiente_pago` y se activa cuando se
  confirma el primer cargo; con plan gratuito queda activa.
- El admin puede crearla manualmente por API (`/api/v1/pack/pack_subscriptions/suscripciones`).
- Los **períodos siguientes** se cobran con `sub_link_pago` o el cron `generar_cargos`
  → cargo `pendiente` → `payment.confirmed` lo marca `pagado` (referencia `sub-<cargo.id>`).

## Reglas
- No prometer cambios de estado sin llamar la tool (`sub_pausar`, `sub_cancelar`, etc.).
- `sub_cancelar` pide el sí del cliente: la primera llamada no ejecuta, deja la pregunta pendiente y
  se la hacés una sola vez; con su sí, repetí la llamada con los mismos datos.
- **Pagar el período:** si el cliente ya tiene una suscripción y pregunta cuánto debe o cómo pagar su
  mensualidad, mirala con `sub_consultar` y mandale el link con `sub_link_pago`. No es una compra nueva:
  no inicies «Contratar un plan» ni le preguntes qué plan quiere.
- **Cambio de plan (subir o bajar):** lo gestionás vos por este chat con el procedimiento «Cambiar de
  plan»; no lo pases a una persona por eso. Cotizá con `sub_cotizar_cambio_plan` y aplicalo con
  `sub_aplicar_cambio_plan`, con el SKU y el `monto_a_pagar` exactos de la cotización: el sistema le
  muestra al cliente el resumen y le pide su «Sí, confirmo». No escribas tu propio resumen ni le
  pidas que confirme antes de llamar `sub_aplicar_cambio_plan`.
- El valor del período sale del snapshot del plan; nunca inventarlo.
