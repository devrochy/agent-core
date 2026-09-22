# ADR-0045 — Planes y cuotas por tenant: catálogo versionado, medición derivada y bloqueo duro

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

agent-core sirve a todos los clientes con el mismo binario, pero no había forma de
venderlo: no existe noción de "plan" ni de límite de uso. Un tenant recién creado es
indistinguible de uno en producción y podía consumir recursos sin cota. Se necesita
que cada tenant opere 1 mes gratis y después se vincule a un plan (basic, premium,
empresarial) con límites de clientes, conversaciones, audio, OCR y packs.

La facturación por uso ya tiene una fuente de verdad: `usage_ledger` append-only
(ADR-0008). Lo que falta es el catálogo de planes, la medición de las demás métricas
y — decisión explícita del operador — un **bloqueo duro** al alcanzar el límite o
vencer el período.

## Decisión

Se introduce billing como componente del core (`core/billing/`), no como pack: es un
concern de plataforma (tenant → nosotros), igual que los costos de Meta, y su
enforcement debe ocurrir en el webhook y el consumer, que son core.

1. **Catálogo de planes en código, espejado a una tabla de sistema.** La fuente de
   verdad es `core/billing/plans.py` (tipada, versionada); `plans` es una **tabla de
   sistema** (`plans`, sin `tenant_id`, sin RLS) sembrada idempotentemente al arrancar
   vía `sync_plans()`, mismo patrón que `plugin_manager.sync_registry()`. Es una
   excepción deliberada a la invariante #1, con el mismo criterio que
   `webhook_events` (ADR-0028) y `meta_app_subscriptions` (ADR-0029): el catálogo es
   global y compartido, no una tabla de negocio. **`app_runtime` (rol del runtime)
   solo tiene `SELECT` sobre `plans`**: define precios y límites compartidos por
   todos los tenants, así que no puede ser escribible desde una sesión de tenant.
   La escritura de `sync_plans()` pasa por `upsert_plan()` **SECURITY DEFINER**
   (dueño), mismo criterio que `purge_tenant`/`admin_tenants`.
2. **Las métricas se derivan de las tablas fuente, no de un contador nuevo.** No se
   crea un ledger paralelo: `clientes` = `customers` activos; `conversaciones` =
   `conversations.started_at` del período; `conversaciones_audio` / `conversaciones_ocr`
   = `messages` inbound con `media_type` de audio/imagen del período; `plugins` = packs
   habilitados del tenant; `mensajes` = `messages` outbound del período;
   `costo_llm_usd` = `SUM(usage_ledger.cost_usd)` del período (ADR-0008). Un registro
   tipado (`core/billing/metrics.py`) declara cada métrica y su consulta, para que
   agregar una no toque el panel ni el enforcement. El conteo del hot path se cachea
   en Redis con TTL corto.
3. **Enforcement por dos niveles.** Plan/trial inválido → el tenant **no operacional**:
   el webhook no encola y el consumer descarta (mismo patrón defense-in-depth que
   `is_tenant_active`, ADR-0001). Cuota alcanzada → se bloquea la **nueva unidad** de
   esa métrica (crear cliente, abrir conversación, procesar audio/imagen, activar un
   pack); las conversaciones ya abiertas siguen respondiendo. El corte se calcula en
   vivo; un cron diario (`core/tasks/billing_lifecycle.py`) solo normaliza estados y
   alimenta avisos.
4. **`tenant_subscriptions`** (negocio: `tenant_id` + RLS, una activa por tenant vía
   índice único parcial) guarda el plan, el estado (`trialing|active|past_due|expired|
   canceled`), el período y el fin del trial. El alta de un tenant crea la suscripción
   `free` con `trial_ends_at = now() + 30d`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Billing como pack instalable | El concern es de plataforma; el enforcement vive en el webhook/consumer (core). Un pack opcional no puede gatear el ingreso de mensajes sin acoplar core→pack (prohibido). |
| Un `usage_counters` propio, incrementado por evento | Duplica la fuente de verdad y puede divergir de las tablas reales; `usage_ledger` ya estableció el criterio "derivar, no contar aparte" (ADR-0008). |
| Planes editables en BD sin espejo de código | Sin una definición versionada, un plan puede quedar en un estado inválido y no hay diff auditable de qué cambió. |
| Bloqueo suave / solo avisar | El operador eligió explícitamente bloqueo duro (decisión de negocio 2026-09-22). |

## Consecuencias

**Positivas:** el uso es auditable y consistente con `usage_ledger`; agregar una métrica
es una entrada en `metrics.py`; el bloqueo es determinístico y no depende de un cron;
los planes se versionan como código (diff y revisión).

**Negativas / lo que aceptamos pagar:** el conteo por métrica es una consulta a las
tablas fuente (mitigada con índice compuesto y cache Redis); el bloqueo duro puede
sorprender a un tenant sin aviso previo, por lo que el panel debe mostrar la barra y
los umbrales 80/100 de forma prominente; la tabla `plans` es una excepción más a la
invariante #1.

**Qué haría que revisáramos esto:** que dos métricas necesiten definiciones distintas
del mismo dato (señal de que el registro de métricas es insuficiente), o que el cache
de conteo cause bloqueos incorrectos (señal de que la derivación debe ser transaccional).

## Impacto en el código

Migración `0034` (`plans`, `tenant_subscriptions`, `subscription_payments`), modelos en
`core/db/models.py`, nuevo paquete `core/billing/` (`plans.py`, `metrics.py`,
`enforcement.py`), `core/onboarding.py` (suscripción free al alta), `core/gateway/webhooks.py`
(gate operacional + audio/OCR), `core/orchestrator/consumer.py` (gate + clientes/conversaciones),
`core/plugin_manager.py` (cota de packs), `core/tasks/billing_lifecycle.py`, panel
`/admin/plan` y barra de progreso. Ver `docs/BILLING.md` y HU-011.
