# ADR-0087 · Camino del dinero en comercio

**Estado**: Aceptada · **Fecha**: 2026-10-02 · **Relacionada con**: ADR-0023, ADR-0067

## Contexto

El link de pago de un pedido lo generaba `generar_link_pago(reference, amount)`
(`pack_payments/skills.py`), donde el **LLM elegía monto y referencia**. Además, al
agregar ítems a un pedido, el link anterior seguía vigente (N-05, HU-COM-01). Un
cobro equivocado es un reclamo, una devolución y, si es a la baja, dinero perdido.

## Decisión

1. **El total manda y vive en el servidor.** La tool de comercio
   `confirmar_pago_pedido(order_id)` calcula el link desde `Order.total`; el LLM
   solo pasa el `order_id`, nunca monto ni referencia (D-SPEC-09-1).
2. **Referencia versionada.** El pedido guarda `payment_reference`,
   `payment_link_url` y `payment_link_version`. Al cambiar el total (agregar ítems),
   la versión sube y la próxima llamada emite `OC-0001-rN`, invalidando el link viejo.
3. **El webhook valida el monto.** `payment.confirmed` con un monto distinto al
   total NO marca el pedido pagado; se registra y se alerta. Un `rejected` tardío
   sobre un pedido ya pagado no cambia su estado.
4. **Monto bajo el mínimo = error de negocio.** La capacidad lanza
   `PaymentCapabilityError`; commerce devuelve `monto_bajo_minimo` sin apagar el
   pack (HU-ARCH-18), y el asistente ofrece transferencia/contraentrega.
5. **Commerce consume `payment_registry` del core**, nunca importa el pack de pagos
   (invariante nº5).

## Justificación

- El contrato de `PaymentCapability` ya existía (ADR-0067); solo faltaba que la
  correlación pedido↔link viviera en el lado que conoce el total.
- Versionar la referencia resuelve la reutilización de links obsoletos sin borrar
  transacciones (historial auditable).

## Consecuencias

- (+) El monto nunca depende del LLM; los links viejos dejan de ser válidos solos.
- (+) El pack de pagos sigue siendo sustituible (Wompi hoy, otro proveedor mañana).
- (−) `generar_link_pago` sigue existiendo en `pack_payments` para consumidores
  genéricos (cobranza, suscripciones), pero **no** hace parte del flujo de comercio.
- **Qué haría que revisáramos esto**: un proveedor que exija una sola referencia por
  pedido sin versiones, o cobros parciales.
