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

## Adenda (2026-10-10): se retira `generar_link_pago`

Revisada por el agente `arquitecto` antes de escribir código.

**Hallazgo** (`auditor-multitenant`, severidad media, no cruza tenants). `generar_link_pago`
recibía `reference` y `amount` del LLM y buscaba las transacciones solo por `tenant_id`:
- un cliente que conociera el número de pedido de otro del mismo negocio (`OC-0002`, secuencial)
  sabía si estaba pagado (`ya_pagado`) y su monto exacto (`monto_no_coincide` lo citaba);
- podía crear un link con esa referencia y un monto inventado, y el `create_link` del dueño del
  pedido (vía `confirmar_pago_pedido`) chocaba después con "Ya existe un cobro…".

**Por qué existía:** la consecuencia (−) de arriba la dejaba "para consumidores genéricos
(cobranza, suscripciones)". No existían: cobranza usa la `PaymentCapability` y suscripciones
`sub_link_pago`. La tool seguía viva porque pack-payments la registraba y el `flow.json` de
pack-commerce la nombraba en "Pedido y cierre" en lugar de `confirmar_pago_pedido`, mientras
`ventas.md` mandaba usar `confirmar_pago_pedido`.

**Decisión:**
1. pack-payments no expone tools al modelo. Aporta la capacidad (ADR-0067), el webhook y la
   configuración del panel. `skills.py` pasa a `proveedor.py` (`tenant_config`, `build_provider`).
2. El flujo de commerce ofrece `confirmar_pago_pedido`, que toma el pedido por `order_id` del
   cliente de la conversación y el monto del servidor. Por número público solo queda
   `consultar_estado_pedido`, que también filtra por cliente.
3. `playbooks/pagos.md` queda sin nombres de tool y sin la regla de `amount`: el monto y la
   referencia los pone el sistema. El archivo se conserva porque los presets lo cargan.

**Descartado:** un contrato del core para que un pack resuelva una referencia contra los pedidos
del cliente (`Cobro`/registro en `core/payments`). Tendría un solo productor (commerce) y un solo
consumidor (esta tool), duplicaría la búsqueda y la regla de versión `-rN` de
`confirmar_pago_pedido` y metería el ciclo de vida del pedido en el core.

**Lo que aceptamos pagar:** cambia la lista de tools que recibe el modelo, así que el gate de
evals se regraba una vez.

**Qué haría que lo revisáramos:** un pack o un plugin de negocio (ADR-0089) que necesite que el
LLM inicie un cobro sin una tool propia que conozca el monto. Ese sería el segundo caso de uso del
resolvedor del lado del servidor, con ADR propio.
