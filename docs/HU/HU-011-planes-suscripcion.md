# HU-011 · Planes, cuotas y suscripción de un tenant

**Como** admin de un tenant, **quiero** ver mi plan, mi consumo y suscribirme a un plan
pago, **para** operar sin sorpresas y desbloquear más capacidad cuando la necesite.

**Como** admin master, **quiero** ver y ajustar el plan de cualquier tenant, **para**
dar soporte (extender un trial, corregir un cobro).

## Criterios de aceptación

- [ ] Todo tenant nuevo queda en plan **free** con 30 días de operación gratuita
      (`trial_ends_at = created_at + 30d`).
- [ ] Existen 4 planes: `free`, `basic`, `premium`, `empresarial`, con límites de
      clientes, conversaciones, conversaciones por audio, conversaciones por OCR,
      plugins, mensajes y costo LLM del período.
- [ ] `/admin/plan` muestra: plan actual, estado (trial/activo/vencido), días
      restantes y una **barra de progreso por métrica** (consumido / límite).
- [ ] El dashboard muestra un resumen del plan y avisa a partir del 80% de una cuota;
      en rojo al 100%.
- [ ] Al alcanzar una cuota se bloquea **la nueva unidad** de esa métrica (crear
      cliente, abrir conversación, procesar audio/imagen, activar pack) con un mensaje
      claro.
- [ ] Al vencer el trial o el período sin pago, el tenant deja de procesar mensajes
      (webhook + consumer) de forma inmediata, sin borrar datos.
- [ ] El tenant puede pulsar **Suscribirme** en un plan pago: se genera un link de pago
      Wompi y, al confirmarse el webhook, la suscripción queda `active` por el período.
- [ ] El panel muestra el historial de pagos (período, monto, estado).
- [ ] Un admin `master` puede cambiar el plan o extender el período de un tenant desde
      `/admin/tenants`, y la acción queda en `audit_log`.
- [ ] Aislamiento: un tenant nunca ve la suscripción ni los pagos de otro (RLS).

## Flujo técnico

`core/billing/plans.py` (catálogo) + `metrics.py` (conteo derivado) + `enforcement.py`
(bloqueo); `core/onboarding.py` crea la suscripción free; `core/gateway/webhooks.py` y
`core/orchestrator/consumer.py` aplican el gate operacional; `core/plugin_manager.py`
aplica la cota de packs; `core/billing/providers/wompi.py` + webhook de billing cierran
el pago. Ver `docs/BILLING.md` y ADR-0045 / ADR-0046.

## Relación con el resto

- Extiende el ciclo de vida del tenant de `docs/MULTITENANT.md` (suspendido manual vs.
  no operacional por billing).
- Se apoya en `usage_ledger` (ADR-0008) como fuente de costo y en `messages.media_type`
  para audio/OCR.
