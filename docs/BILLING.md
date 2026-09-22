# Planes, cuotas y facturación de tenants

Última actualización: 2026-09-22 · ADR-0045 (planes/cuotas/enforcement) · ADR-0046 (cobro Wompi)

Este documento describe cómo agent-core vende y limita el servicio a cada tenant. Es el
equivalente de `MULTITENANT.md` para el plano comercial: aquí vive qué es un plan, cómo
se mide el consumo, qué pasa al llegar al límite y cómo paga un tenant.

## 1. Conceptos

- **Plan:** catálogo de capacidad. Se define en código (`core/billing/plans.py`) y se
  espeja a la tabla de sistema `plans`.
- **Suscripción:** relación de un tenant con un plan (`tenant_subscriptions`, con RLS).
  Una activa por tenant.
- **Período:** ventana mensual de la suscripción (`period_start` → `period_end`). El
  trial del plan free termina en `trial_ends_at`.
- **Cuota:** límite de una métrica dentro del período (o lifetime, según la métrica).
- **Métrica:** unidad medible del consumo (clientes, conversaciones, audio, OCR, packs,
  mensajes, costo LLM).

## 2. Catálogo de planes

Precios en COP/mes. Wompi exige un mínimo de **150.000 COP por link de pago**: los
planes pagos se fijan por encima. `null` = ilimitado.

| Plan | Precio/mes | Clientes | Conversaciones | Audio | OCR | Plugins | Mensajes | Costo LLM (USD) |
|---|---|---|---|---|---|---|---|---|
| `free` | 0 (30 días) | 100 | 500 | 50 | 20 | 1 | 2.000 | 5 |
| `basic` | 200.000 | 500 | 3.000 | 300 | 150 | 3 | 15.000 | 50 |
| `premium` | 600.000 | 2.000 | 15.000 | 1.500 | 800 | 8 | 80.000 | 200 |
| `empresarial` | 1.800.000 | ∞ | ∞ | ∞ | ∞ | todos | ∞ | ∞ |

El plan `free` es el estado inicial de todo tenant nuevo y **también** el trial: 30 días.
No se paga; al vencer `trial_ends_at` el tenant deja de operar hasta que se suscriba.

Los límites viven en `PlanLimits` (modelo validado) dentro del catálogo, y se siembran
en `plans.limits` (JSONB) con `sync_plans()`. Editar un plan = editar el catálogo y
volver a sincronizar (queda en el diff de git y se audita).

## 3. Métricas

Cada métrica se **deriva** de la tabla que ya es su fuente de verdad (no hay un contador
paralelo, ADR-0045). El registro vive en `core/billing/metrics.py`.

| Clave | Qué cuenta | Fuente | Alcance |
|---|---|---|---|
| `clientes` | clientes activos del tenant | `customers` (`deleted_at is null`) | lifetime |
| `conversaciones` | conversaciones iniciadas | `conversations.started_at` | período |
| `conversaciones_audio` | audios entrantes procesados | `messages` inbound con `media_type like 'audio/%'` | período |
| `conversaciones_ocr` | imágenes entrantes procesadas | `messages` inbound con `media_type like 'image/%'` | período |
| `plugins` | packs habilitados | `AgentSpec.packs` / packs activos | lifetime |
| `mensajes` | mensajes salientes enviados | `messages` outbound | período |
| `costo_llm_usd` | gasto LLM del período | `SUM(usage_ledger.cost_usd)` (ADR-0008) | período |

El conteo del camino caliente (webhook) se cachea en Redis con TTL corto. La definición
de cada métrica (tabla, filtro, alcance) es la única fuente; el panel y el enforcement
la consumen.

## 4. Enforcement (bloqueo duro)

Hay dos niveles, ambos deterministas y calculados en vivo (no dependen del cron):

**Nivel tenant — no operacional.** Si el plan/ trial venció o la suscripción no está
`active`/`trialing`, el tenant deja de procesar mensajes:
- el webhook no encola (`core/gateway/webhooks.py`),
- el consumer descarta los eventos ya encolados (`core/orchestrator/consumer.py`),

mismo patrón defense-in-depth que la suspensión manual (`is_tenant_active`,
`docs/MULTITENANT.md`). `Tenant.status` (suspensión manual) y el estado de billing son
independientes: un tenant puede estar `active` y aun así no ser operacional por falta de
pago.

**Nivel acción — cuota alcanzada.** Se bloquea la **nueva unidad** de la métrica:

| Métrica | Punto de bloqueo |
|---|---|
| `clientes` | alta de un `Customer` nuevo (`consumer.py`) |
| `conversaciones` | apertura de una `Conversation` nueva (`consumer.py`) |
| `conversaciones_audio` | procesamiento de audio (`webhooks.py::_try_process_media`) |
| `conversaciones_ocr` | procesamiento de imagen (`webhooks.py::_try_process_media`) |
| `plugins` | activación de un pack (`plugin_manager.activate`) |
| `mensajes` / `costo_llm_usd` | envío / cabecera del turno |

Las conversaciones **ya abiertas** siguen respondiendo: el corte es a la creación de
nuevas unidades, no un apagado a mitad de una charla. El panel avisa desde el 80% y
marca en rojo el 100%, para que el bloqueo no sea sorpresa.

El cron diario `core/tasks/billing_lifecycle.py` normaliza estados (`trialing` vencido →
`expired`, período vencido → `past_due`) y alimenta los avisos del panel. Si el cron no
corre, el enforcement igual bloquea porque se evalúa al momento.

## 5. Alta y ciclo de vida

1. `agent-core tenant create` / `/admin/tenants/nuevo` → `onboard_tenant` crea la
   suscripción `free` con `trial_ends_at = now() + 30d`.
2. El tenant opera gratis 30 días con las cuotas de `free`.
3. Al vencer el trial: no operacional. El panel invita a suscribirse.
4. `POST /admin/plan/subscribe` con un plan pago → link Wompi → webhook → `active` por
   el período.
5. Antes de `period_end`: el panel ofrece **Renovar** (nuevo link). Sin pago, al vencer
   pasa a `past_due`/`expired` y se corta.

Un admin `master` puede cambiar el plan o extender el período desde `/admin/tenants`
(soporte); la acción se registra en `audit_log`.

## 6. Pago con Wompi

- El cobro de **plataforma** usa credenciales propias (`.env`), nunca las del tenant.
  El modelo del pack `pack_payments` cobra a los clientes del tenant y es otra cosa
  (ver ADR-0046).
- Flujo: `subscription_payments` (pending) → `create_payment_link` → checkout hospedado
  de Wompi → `POST /webhooks/billing/wompi` (firma + idempotencia) → al aprobar,
  `active` + `period_end`.
- No se almacenan datos de tarjeta.
- **Diferido:** débito recurrente automático (fuente de pago). La interfaz
  `BaseBillingProvider` (`core/billing/providers/base.py`) deja el enchufe.

Configuración (`.env`):

```
BILLING_PROVIDER=wompi          # wompi | none
BILLING_CURRENCY=COP
WOMPI_BILLING_PUBLIC_KEY=
WOMPI_BILLING_PRIVATE_KEY=
WOMPI_BILLING_EVENTS_SECRET=
WOMPI_BILLING_SANDBOX=true
BILLING_TRIAL_DAYS=30
```

## 7. Cómo extender

**Agregar una métrica:** una entrada en `core/billing/metrics.py` (clave, etiqueta,
unidad, consulta fuente, alcance) + su punto de enforcement + su campo en `PlanLimits`.
El panel y el cálculo de progreso se generan solos.

**Agregar o cambiar un plan:** editar `core/billing/plans.py`, correr `sync_plans()`.
Queda versionado en git y auditado.

**Agregar un proveedor de pago:** implementar `BaseBillingProvider` en
`core/billing/providers/` y seleccionarlo con `BILLING_PROVIDER`.

## 8. Seguridad y aislamiento

- `plans` es una tabla de **sistema** (sin RLS, catálogo global), pero `app_runtime`
  solo tiene `SELECT`: los precios/límites compartidos no son alterables desde una
  sesión de tenant. `sync_plans()` escribe vía `upsert_plan()` SECURITY DEFINER.
- `tenant_subscriptions` y `subscription_payments` son de negocio (`tenant_id` +
  RLS + índice que empieza por `tenant_id`).
- El webhook de billing llega sin tenant: resuelve el suyo con
  `resolve_subscription_payment_tenant()` (SECURITY DEFINER, devuelve solo el
  `tenant_id`), sin enumerar tenants ni exponer datos de otro.
- El webhook de WhatsApp de un tenant verifica la firma **antes** del gate de
  billing; si no, un visitante sin credenciales podría distinguir un tenant
  operativo de uno vencido.

## 9. Pendiente (fuera de esta entrega)

- Débito recurrente automático (ADR-0046, punto 4).
- Prorrateo al cambiar de plan a mitad de período.
- Excedentes (overage) y planes por volumen.
- Stripe u otro proveedor internacional.

## Referencias

- ADR-0045 planes/cuotas/enforcement · ADR-0046 cobro Wompi
- ADR-0008 `usage_ledger` · ADR-0028/0029 tablas de sistema
- `docs/MULTITENANT.md` (ciclo de vida) · `docs/HU/HU-011-planes-suscripcion.md`
- `core/billing/` · `core/onboarding.py` · `core/tenants.py`
