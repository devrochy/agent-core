# ADR-0067 — Capacidad de pagos del core y cobro digital en cobranza

- **Estado:** aceptado
- **Fecha:** 2026-09-25

## Contexto

Cobranza necesitaba **cobro digital** (link/QR de una cuota) reutilizando el pack de
pagos (`pack_payments`, Wompi) cuando esté activo, y con un **fallback informativo**
(cuenta de transferencia configurable) cuando no lo esté. El invariante nº5 prohíbe
que un pack importe a otro, y no existía forma de que un pack creara un link de pago
de otro ni un evento de confirmación compartido: los topics `ON_PAYMENT_*` vivían
**dentro** de `pack_payments`, y la creación de links solo estaba en su skill.

## Decisión

1. **Capacidad de pagos en el core** (`core/payments/`), con el mismo molde que
   `LeadRegistry` (ADR-0056): `PaymentMethod`, `PaymentLink`, protocolo
   `PaymentCapability` (`methods()`, `create_link(reference, amount, description,
   method)`) y `payment_registry.register(plugin_id, capability)` /
   `capability_for(enabled_packs)`. El core **no importa packs**; los packs se
   registran desde su `on_activate()`.
2. **Topics compartidos en el core**: `ON_PAYMENT_CONFIRMED`/`ON_PAYMENT_REJECTED`
   se mueven a `core/shared/events.py`. `pack_payments` los emite (su webhook ya
   lo hacía) y `pack_cobranza` se **suscribe** para registrar el cobro.
3. **`pack_payments` aporta la capacidad** (`WompiPaymentCapability`): un método
   "pago en línea / QR" (Wompi resuelve tarjeta/PSE/Nequi/Bancolombia en el
   checkout). Crea el link y la `PaymentTransaction` (para que el webhook
   correlacione por `payment_link_id`).
4. **`pack_cobranza` consume o cae al informativo**:
   - Con capacidad: `POST /gestor/carteras/{id}/cuotas/{cid}/link-pago` crea el
     link/QR (referencia `cob-<cuota_id>`), guarda `LinkPago` (extendido con
     `cuota_id`/`prestamo_id`) y se **suscribe** a `ON_PAYMENT_CONFIRMED` para
     registrar el `Pago` (idempotente), marcar la cuota pagada y mover caja/saldo.
   - Sin capacidad: devuelve la **cuenta informativa** de `CobranzaConfig`
     (titular, banco, tipo/número, alias) y el gestor registra el pago manual.
5. **QR generado por el core** (`qr_png_data_uri`, con `qrcode[png]`): el pack
   devuelve el QR como **PNG data URI**; la app lo muestra sin librería de QR.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `pack_cobranza` importa `pack_payments` | Viola el invariante nº5 |
| Duplicar otro cliente Wompi en cobranza | Ya era el patrón de ADR-0046, pero multiplica el mantenimiento y no da auto-confirmación cruzada |
| Un tercer registry solo para cobranza | El core es el lugar neutral; el molde (`LeadRegistry`) ya existe |
| QR con librería en la app | Dependencia nueva en el cliente; el PNG del core sirve para link e informativo por igual |

## Consecuencias

**Positivas:** cobranza cobra en línea con la pasarela del tenant sin acoplarse a
otro pack; confirmación automática por EventBus; un solo lugar define el contrato de
pagos; el QR viaja como PNG (sin dep en la app); fallback informativo cuando no hay
pasarela.

**Negativas / lo que aceptamos pagar:** el core gana un registry más; la
confirmación depende de que `pack_payments` emita (no idempotente en el emisor, pero
el handler de cobranza sí — reentrega de webhook no duplica el pago); el QR se
genera en el servidor (unos bytes más por respuesta).

**Qué haría que revisáramos esto:** que otro pack necesite pagos (→ más capacidades),
o que Wompi incorpore métodos que exijan parámetros por método.

## Impacto en el código

- `core/payments/` (nuevo), `core/shared/events.py`, `pyproject.toml` (`qrcode[png]`).
- `packages/pack-payments/pack_payments/payments.py` (nuevo) + `module.py`.
- `packages/pack-cobranza/pack_cobranza/pagos.py` (nuevo), `config.py`, `models.py`,
  migración `0011`, `api_gestor.py`, `module.py`.
- `solvershub-apk`: tipos, API y acción "Cobro con QR" en el detalle de cuota.

## Adenda (2026-10-10): el link vencido se reemplaza y el rechazo trae un código

Revisada por el agente `arquitecto` antes de escribir código.

**Hallazgo.** Al retirar `generar_link_pago` (adenda de ADR-0087), el vencimiento y el reemplazo
del link quedaron en la tool. `WompiPaymentCapability.create_link`, el camino de
`confirmar_pago_pedido`, de cobranza y de suscripciones, devolvía el link `pendiente` aunque ya
hubiera vencido. Además, todo `PaymentCapabilityError` era igual para el consumidor. Commerce
contestaba `monto_bajo_minimo` también a un pedido ya pagado o sin pasarela configurada.

**Decisión:**
1. **`create_link` no devuelve un link vencido.** El link vence a las `created_at +
   link_expira_horas`, con 15 min de margen. Se calcula con la vigencia configurada hoy, no con la
   que había al crear el link: si el negocio la cambia, durante esa ventana un link puede contarse
   como vigente cuando Wompi ya lo expiró, o al revés. Un link vencido se reemplaza por uno nuevo,
   también si era por otro monto (el saldo de una cuota después de un abono). La transacción vieja
   queda con `deleted_at` (y `expirado` si estaba pendiente) para auditoría y para el webhook
   tardío. El `update` exige que siga como se leyó; si otra llamada o un webhook la cambió, se
   relee y se decide de nuevo. El link nuevo de Wompi queda sin usar y vence solo. Dos primeras
   llamadas a la vez chocan con el índice único parcial (`pack_payments_0003`) y la segunda
   devuelve el link de la primera. El Protocol `PaymentCapability` y `PaymentLink` no cambian.
2. **Código en el rechazo.** `PaymentCapabilityError` ya admitía `code` (por `BusinessError`,
   ADR-0088). `core/payments` publica el vocabulario y el consumidor ramifica por él sin importar
   el pack de pagos (invariante nº5):

   | Código | Cuándo |
   |---|---|
   | `monto_bajo_minimo` | La pasarela no cobra un monto tan bajo |
   | `sin_pasarela` | El negocio no configuró la pasarela |
   | `ya_pagado` | La referencia tiene un pago aprobado (en cualquier transacción, también una reemplazada) |
   | `en_revision` | Llegó un pago por otro monto y lo revisa el negocio |
   | `monto_no_coincide` | La referencia tiene un link **vigente** por otro monto |
   | `no_disponible` | La pasarela rechazó o falló al crear el link (también 401 y 5xx: se loguea como error) |

   Sin código, el consumidor no supone el motivo.
3. **Commerce** (`confirmar_pago_pedido`) contesta según el código: `monto_bajo_minimo` igual que
   antes (ADR-0087 §4); `ya_pagado` sin ofrecer otro medio de pago; `en_revision` y
   `monto_no_coincide` con `escalate`, que el core aplica sin depender del modelo; el resto como
   pago en línea no disponible por ahora.

**Seguimiento (fuera de esta adenda).** Cobranza y suscripciones siguen tratando todo rechazo como
`monto_bajo_minimo`. Suscripciones devuelve `cargo.link_url` guardado sin llamar a `create_link`
(`_link_de_cargo`, `sub_link_pago`), así que todavía puede entregar un link vencido. Como
`create_link` es idempotente, basta con quitar ese atajo.

## Adenda (2026-10-25): el mínimo por cobro lo declara la capacidad, no el consumidor

**Hallazgo.** `pack_commerce` guardaba su propia cifra (`pago_en_linea_monto_minimo`, 150.000 COP) para
rechazar un pedido en línea antes de generar el link. Esa cifra salió de un 422 del **sandbox** de Wompi;
en producción el mínimo publicado es 1.500 COP. Como el default valía igual en producción, un plan de
80.000 pagado en línea salía rechazado salvo que cada negocio lo bajara a mano, y el playbook de ventas
repetía el 150.000 como si fuera el mínimo real.

**Decisión.**
1. **Operación opcional `monto_minimo()`** en la capacidad, con el mismo molde que `procesar_evento`
   (se invoca con `getattr`): devuelve el mínimo por link del ambiente del negocio, o `None` si no lo
   sabe. `core.payments.monto_minimo(capacidad)` la expone a los packs sin importar `pack_payments`
   (invariante nº5). El Protocol `PaymentCapability` no cambia.
2. **`WompiPaymentCapability.monto_minimo()`** devuelve 150.000 con `sandbox` y 1.500 en producción
   (`min_amount_cop`).
3. **`pack_commerce`**: `pago_en_linea_monto_minimo` pasa a `None` por defecto. Vacío, el gate de
   `crear_pedido` usa el mínimo de la pasarela; un valor explícito (incluido 0, que apaga el gate) manda
   sobre el de la pasarela. Sin pasarela que lo declare queda 150.000, la política anterior.
4. **Playbook de ventas** (`pack_commerce` 2.5.0): ya no cita una cifra; el modelo se apoya en el error
   de `crear_pedido` y nunca nombra un mínimo.

Fuera de esta adenda: el cobro de la plataforma (`core/billing/providers/wompi.py`, `MIN_AMOUNT_COP`) y
el override de `solvershub-comercial`, que tienen su propio texto.
