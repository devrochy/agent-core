# ADR-0033 — Contabilidad de costes de Meta: dos tablas, dos fuentes de verdad, nunca una tarifa local

- **Estado:** aceptado
- **Fecha:** 2026-09-20

## Contexto

El ticket pide ingesta de costos de WhatsApp desde dos fuentes: el webhook
`statuses` (tiempo real, refleja recategorizaciones) y `pricing_analytics`
(diario, para cuadrar con la factura). La regla innegociable es no calcular
nunca un costo en dólares localmente -- los volume tiers se agregan a nivel
de business portfolio, no de WABA, así que un panel multi-tenant no puede
predecir la tarifa unitaria de un tenant.

Esto tiene una consecuencia de diseño no obvia: el objeto `pricing` del
webhook `statuses` **nunca trae un monto en dólares** (solo
`billable`/`pricing_model`/categoría) -- `pricing_analytics` es la ÚNICA
fuente que trae `cost`. El "panel de diferencia entre webhooks y
pricing_analytics" pedido por el ticket no puede comparar dólares
estimados contra dólares reales (el lado webhook no tiene dólares), así
que se diseñó como una comparación de VOLUMEN: cuántos eventos facturables
vio el webhook por categoría vs. el `volume` que reporta `pricing_analytics`
para esa misma categoría/período.

## Decisión

1. **Dos tablas nuevas**, tal como pide el ticket: `message_costs`
   (webhook, factual sin dólares) y `pricing_daily` (diario,
   `cost` autoritativo). No se agregan columnas de pricing a `Message` --
   cada evento de pricing es un HECHO nuevo, no un estado a sobreescribir
   (una fila por evento, sin unique constraint sobre `external_message_id`).
2. **`apply_status_update` gana un segundo efecto**, no un módulo aparte:
   cuando el status trae `pricing`, persiste `MessageCost` en el mismo
   `tenant_scope`/tenant ya resuelto por `resolve_tenant_for_message`
   (ADR-0027) -- nunca se re-deriva el tenant desde otra fuente.
3. **`pricing_analytics` es un GET sobre el nodo de la WABA**
   (`GET /{waba_id}?fields=pricing_analytics.start(...).end(...)
   .granularity(DAILY).dimensions(PRICING_CATEGORY,PRICING_TYPE,TIER,COUNTRY)`),
   con las `dimensions` SIN corchetes en el query string. Se documenta en
   el código la tabla de `granularity` NO intercambiable entre
   `analytics`/`conversation_analytics`/`pricing_analytics` -- fuente de
   error señalada explícitamente por el ticket.
4. **Enums no verificados se guardan igual**, con `log.warning` si no
   coinciden con el set de valores documentados conocidos -- nunca se
   rechaza la ingesta por un valor inesperado (CLAUDE.md invariante nº7).
5. **Extended credits: solo lectura.** `get_extended_credits` expone
   ÚNICAMENTE el GET -- Meta no soporta create/update/delete sobre
   `extendedcredits`, y `owning_credit_allocation_configs` queda
   explícitamente fuera de alcance (exige ser Solution Partner). El panel
   no le atribuye a la respuesta un significado de "método de pago
   configurado: sí/no" que Meta no da.
6. **Business Agent (facturación por token, 1-ago-2026): sin pipeline de
   ingesta.** El ticket no da ninguna API para este dato -- se documenta
   como aviso informativo en el panel, nunca como una tabla o sync
   inventados sin fuente real.
7. **Franquicia de 1000 mensajes/mes: feature flag apagado por defecto**
   (`meta_service_message_allowance_hypothesis_enabled`). Si se prende,
   solo afecta un contador informativo -- nunca resta nada del `cost`
   autoritativo mostrado.

## Hallazgo del auditor-multitenant (2026-09-20) corregido

Severidad baja: el índice único `uq_pricing_daily_dimensions` no incluía
`tenant_id` en su clave -- no explotable hoy (`waba_account_id` ya es 1:1
con un tenant, y `sync_pricing_daily` filtra `tenant_id` explícito en su
query de upsert), pero si esa garantía de esquema se relajara algún día,
la clave sin `tenant_id` permitiría una colisión entre tenants a nivel de
constraint de BD. Se agregó `tenant_id` como primera columna de la clave.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Comparar dólares estimados (webhook) vs. reales (pricing_analytics) | El webhook nunca trae dólares -- inventar una estimación local violaría la regla innegociable del ticket. |
| Agregar columnas de pricing a `Message` en vez de una tabla aparte | Un mensaje puede recibir varios eventos de pricing (sent+delivered con pricing distinto); una tabla de hechos append-only es más correcta que sobreescribir columnas. |
| Implementar `owning_credit_allocation_configs` | El ticket lo excluye explícitamente salvo ser Solution Partner. |

## Consecuencias

**Positivas:** el monto en dólares que ve el usuario siempre viene de Meta,
nunca de un cálculo local que podría estar mal si cambian tiers/tarifas sin
avisar; las recategorizaciones de plantilla quedan visibles sin reescribir
retroactivamente lo ya facturado.

**Negativas / lo que aceptamos pagar:** el panel de "diferencia" compara
cantidades, no dólares -- requiere que el usuario interprete un desajuste
de volumen como señal de alerta, no como un monto a reclamar directamente.

**Qué haría que revisáramos esto:** si Meta publica una API real para el
costo de Business Agent por token, se agrega como una fuente más de
`pricing_daily` (la columna `category` ya deja espacio para
`"business_agent_tokens"` sin romper el esquema).

## Impacto en el código

- `alembic/versions/0028_meta_pricing_costos.py`, `core/db/models.py`
  (`MessageCost`, `PricingDaily`).
- `core/gateway/meta_message_status.py`, `core/gateway/webhooks.py`.
- `core/gateway/meta_pricing_analytics.py`, `core/gateway/meta_business_credits.py`.
- `core/tasks/meta_pricing_sync.py`, `core/tasks/celery_app.py`.
- `core/admin/views.py`, `core/admin/templates/costs.html`,
  `core/admin/templates/costs_meta_creditos.html`.
