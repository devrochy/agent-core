# ADR-0083 — `pack_subscriptions`: suscripción de cliente final sobre planes (Opción 1, opcional)

- **Estado:** aceptado
- **Fecha:** 2026-09-30
- **Relacionado:** ADR-0082 (cartera recurrente, base), ADR-0019 (enablement por pack), ADR-0080

## Contexto

El pack-commerce modela `Product`/`ProductVariant` con **precio de pago único**
(`packages/pack-commerce/pack_commerce/models.py:107-132`) y `Order` como compra one-shot
(`models.py:202-234`). No hay ningún concepto de **suscripción del cliente final** a un
plan: ni ciclo, ni renovación, ni estado, ni prueba.

Los **planes de la plataforma** (Free/Basic/Premium/Empresarial) existen, pero viven en
código (`core/billing/plans.py:49-115`) y en la tabla de sistema `plans`, y su cobro es
plataforma→tenant (`TenantSubscription`, Wompi de plataforma). No son productos del
catálogo ni son visibles al asistente.

Hoy el tenant `solvershub` “vende planes” **forzando** el catálogo: los planes se cargan
como productos y el asistente responde con `consultar_inventario` (lo que esperan los
evals `05-info-plan-basic` y `07-comprar-plan-basic`). Funciona para **informar**, pero no
para **gestionar una suscripción** (alta, renovación, cancelación, prueba de 30 días).

La Opción 1 (ADR-0082 §Alternativas) pide un concepto **nuevo** de suscripción de cliente
final, pero debe ser **opcional**, activable por tenant, sin obligar a quien solo usa la
cartera recurrente (Opción 2).

## Decisión

Crear un **pack de negocio opcional** `pack_subscriptions` (activado por `spec.packs` como
cualquier pack, ADR-0019), que **depende solo del core** (regla: ningún pack importa a otro)
y reusa el `Customer` del core y el catálogo cuando está habilitado.

1. **Modelos** (tablas con prefijo `pack_subscriptions_`, RLS, `tenant_id`):
   - `Plan` — oferta de suscripción por tenant: `nombre`, `descripcion`,
     `precio` `Numeric`, `moneda`, `periodicidad` (`mensual|anual`), `trial_dias`,
     `limites` JSONB (entitlements), `activo`, `producto_id`/`variante_id` **opcional**
     (si se quiere espejar el catálogo de commerce).
   - `Subscription` — `customer_id` (lógico a core), `plan_id`, `estado`
     (`trialing|activa|pausada|cancelada|vencida`), `periodo_actual_inicio/fin`,
     `proximo_cargo`, `trial_termina`, `fecha_alta/cancelacion`.
   - `SubscriptionCharge` — cargo por período: `periodo`, `monto`, `estado`
     (`pendiente|pagado|rechazado`), `link_pago`/`referencia`.
2. **Tools del asistente** (registradas solo si el pack está habilitado):
   - `sub_informar_planes` — lista planes/precios/límites reales del tenant.
   - `sub_consultar_suscripcion`, `sub_crear_suscripcion`, `sub_cambiar_plan`,
     `sub_pausar`, `sub_cancelar`, `sub_activar_prueba`.
3. **Cobro** por período: emite `LinkPago` (vía `pack_payments` si está habilitado) o queda
   `pendiente` para cobro manual. El **recurrente automático** (tarjeta/débito) queda fuera
   (ADR de débito recurrente diferido).
4. **Onboarding**: el pack aporta su `flow.json`, playbook y `collaborator_roles` si aplica;
   su seed de planes es parte del alta (ver REQ de seed genérico por pack).
5. **Convivencia**: si el tenant no activa el pack, no existe ninguna tool `sub_*` y el
   asistente sigue con la Opción 2 (cartera) o con el catálogo de commerce.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Meterlo en pack-commerce | Acopla ventas one-shot con recurrencia; el pack crece en dos modelos de cobro |
| Reusar `core/billing` (planes de plataforma) | Es el cobro plataforma→tenant, no del cliente final; no es producto ni ve al cliente |
| Hacerlo obligatorio en todos los tenants | No todos venden suscripciones; rompe el invariante de personalización por pack |
| Implementarlo primero (antes que Opción 2) | La Opción 2 ya tiene modelo y precedente; la 1 es nueva y opcional |

## Consecuencias

**Positivas:** suscripciones de cliente final como capacidad **activable**; el asistente
informa/gestiona planes con tools reales; no se toca el core ni otros packs; los planes del
tenant dejan de depender de “engañar” al catálogo.

**Negativas / lo que aceptamos pagar:** un pack más (migraciones, tools, playbook, evals);
solapamiento con `core/billing` (dos “planes”: plataforma y tenant) que hay que nombrar
claro para no confundir; el cobro recurrente real queda pendiente.

**Qué haría que revisáramos esto:** que el negocio necesite cobro recurrente automático, o
que la distinción cobranza (Opción 2) vs venta (Opción 1) se difumine en un solo modelo.

## Impacto en el código

- Nuevo `packages/pack-subscriptions/` (models, skills, module, flow.json, playbooks,
  migraciones, config, tests) + entry point.
- `spec.packs` del tenant `solvershub` (activación) y flow del tenant.
- `docs/PLUGIN-CONTRACT.md` (nuevo pack) y catálogo de packs.
