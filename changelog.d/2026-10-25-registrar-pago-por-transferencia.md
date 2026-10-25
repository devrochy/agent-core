## Registrar el pago por transferencia de un cobro de suscripción (HU-SUB-26)

El dueño puede marcar pagado el cobro que el cliente transfirió. Sin esto el cargo seguía `pendiente`, el cliente recibía recordatorios y terminaba en pausa por mora.

- `POST /api/v1/pack/pack_subscriptions/cargos/{id}/registrar-pago` con `{referencia}` (número del comprobante o texto): reusa `registrar_pago_externo` del proveedor (canal `transferencia`), reactiva la suscripción si estaba vencida o pausada por mora y emite `subscription.charge_paid` como un pago por link. Idempotente por referencia; un cargo ya pagado con otra referencia, anulado, de cambio de plan o de una suscripción cancelada responde 409. Permiso de escritura y auditoría (`subs_pago_transferencia_registrado`).
- `paga_por_otro_canal` ya no trata la transferencia como «lo cobra otro canal»: el pack sigue generando el link y los avisos del período siguiente. Solo el canal de la plataforma los apaga.
- `registrar_pago_externo` acepta `emitir_pago` (por defecto falso: el cobro de la plataforma no cambia).
