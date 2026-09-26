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
