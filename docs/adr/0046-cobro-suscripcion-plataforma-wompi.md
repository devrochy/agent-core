# ADR-0046 — Cobro de la suscripción de plataforma vía Wompi (link por período)

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

Con ADR-0045 existen planes y cuotas, pero un tenant que quiere salir del plan free
necesita una forma de pagar. El repo ya tiene un proveedor Wompi probado contra el
sandbox real (`packages/pack-payments/providers/wompi.py`) — pero ese cliente cobra a
los **clientes del tenant** (pedidos), no al tenant por su plan. Son dominios distintos:
las credenciales, las tablas y quién paga son otros.

## Decisión

El cobro del plan lo hace el **core** (`core/billing/providers/`), con credenciales de
la **plataforma** (nuestro Wompi) tomadas de `settings`, nunca del tenant:

1. **Link de pago de un solo uso por período.** El tenant pulsa "Suscribirme" → se crea
   un `subscription_payments` (pending) y se genera un link Wompi (`create_payment_link`
   con `reference` correlacionable). Se redirige al checkout hospedado de Wompi. No se
   almacenan tarjetas ni datos de pago.
2. **Webhook propio `POST /webhooks/billing/wompi`**, separado de los webhooks de
   tenant. Verifica la firma con el `events_secret` de plataforma, es idempotente
   (por referencia + evento) y, al aprobarse, activa/extiende la suscripción
   (`period_end = now() + intervalo`) y registra el pago. Llega sin `tenant_id`, así
   que el tenant del pago se resuelve con una función **SECURITY DEFINER**
   (`resolve_subscription_payment_tenant`) que devuelve solo el `tenant_id` — nunca
   se enumeran tenants ni se expone contenido del pago a otro tenant. El webhook de
   WhatsApp de un tenant verifica la firma ANTES del gate de billing (si no, el
   estado de facturación se filtra a un visitante sin credenciales). Los eventos se
   auditan en `webhook_events` (tabla de sistema, ADR-0028) con
   `provider='wompi_billing'`.
3. **El cliente Wompi se replica en core, no se importa del pack.** Core no puede
   depender de un pack (regla de capas) e igual los dominios difieren. Se copia el
   algoritmo ya verificado (body de `POST /v1/payment_links`, verificación de firma,
   mínimo real de Wompi) y se documenta como duplicación deliberada.
4. **Recurrente automático (débito a fuente de pago) queda diferido y documentado.**
   Fase actual: link + renovación asistida desde el panel. La interfaz `BaseBillingProvider`
   deja el enchufe para Stripe u otro proveedor.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Reusar `pack_payments` / su tabla `transactions` | Mezcla el cobro de plataforma con el del negocio del tenant; core no puede importar el pack. |
| Stripe desde el inicio | El operador eligió Wompi (local, ya probado); Stripe exige cuenta e integración nuevas. |
| Cobro recurrente automático ya | Más superficie (guardar fuente de pago, PCI, capacidades del proveedor); se difiere explícitamente. |
| Facturación 100% manual sin pasarela | No cumple el objetivo de autoservicio del tenant. |

## Consecuencias

**Positivas:** self-service sin intervención manual; sin datos de tarjeta en el sistema;
reutiliza un algoritmo Wompi ya validado contra el sandbox real; el webhook separado
mantiene aislado el pago de plataforma del tráfico de los tenants.

**Negativas / lo que aceptamos pagar:** el link de Wompi exige un monto mínimo de
150.000 COP, así que el precio mensual de un plan no puede ser menor (se fijan precios
por encima del mínimo, o se cobra trimestral/anual); la renovación no es automática y
depende de un recordatorio + acción del tenant; duplicación del cliente Wompi.

**Qué haría que revisáramos esto:** que el volumen de tenants haga insostenible la
renovación manual, o que Wompi habilite suscripciones recurrentes nativas — entonces se
implementa el diferido (punto 4).

## Impacto en el código

`core/billing/providers/{base,wompi}.py`, `core/billing/checkout.py`,
`core/config.py` + `.env.example` (creds de plataforma), `core/admin/views.py`
(`/admin/plan`, `/admin/plan/subscribe`) y la ruta de webhook de billing, tabla
`subscription_payments` (migración `0034`). Ver `docs/BILLING.md` y HU-011.
