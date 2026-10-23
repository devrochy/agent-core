# ADR-0106 — Suscripciones v1: contrato único, estados y tools del core

- **Estado:** aceptado
- **Fecha:** 2026-10-07
- **Supersede/enmienda:** ADR-0082 (suscripción de cartera como Opción 2), ADR-0083 (`pack_subscriptions` como
  Opción 1). Los dos quedan como **historias de los proveedores**; este ADR fija el contrato que ambos implementan.
- **Contexto:** HU-SUB-01 (D-SPEC-06-1, D-15 y D-16): un solo juego de tools para el LLM y un contrato para los
  dos proveedores, para que un negocio cambie de cobro en línea a cobro en campo sin tocar su catálogo ni su asistente.

## Contexto

Existían **dos motores paralelos** de suscripción: `pack_subscriptions` (ADR-0083, planes como productos de
commerce, con las tools `sub_consultar_suscripcion`, `sub_pausar_suscripcion`, `sub_cancelar_suscripcion`,
`sub_renovar_suscripcion` y `sub_link_pago`) y el modo suscripción de la cartera de cobranza (ADR-0082,
`cob_*_suscripcion`, HU-SUB-15). Cada mejora se hacía dos veces y el LLM podía ver dos juegos de tools para la
misma acción. El contrato del core (`core/subscriptions`) solo cubría la activación desde commerce.

## Decisión

1. **Contrato único** en `core/subscriptions`: `SubscriptionCapability` con **8 operaciones**
   (`activate`, `get`, `list_for_customer`, `pause`, `resume`, `cancel`, `change_plan`, `charge_link`) y
   `provider_id`. Los DTO del contrato: `PlanSnapshot` (`plan_code`, `nombre`, `precio`, `moneda`,
   `periodicidad` en `mensual|trimestral|anual`, `trial_dias`, `entitlements`), `SubscriptionView` y `ChargeLink`
   (`url`, `referencia`, `periodo`). Los errores de negocio son `SubscriptionError` (HU-ARCH-18, resultado
   controlado; códigos `monto_bajo_minimo`, `cliente_no_encontrado`, `periodo_futuro`, `ya_pagado`).
2. **Selección del proveedor**: `subscription_registry.capability_for_tenant(tenant_id, provider=None)` resuelve
   entre los proveedores **habilitados** para el tenant; si hay dos sin elección explícita, lanza
   `SubscriptionProviderAmbiguity` con mensaje claro (configuración visible en el panel). Con `provider` se
   fuerza uno y se valida que esté registrado y habilitado.
3. **Tools del LLM en el core**: un solo juego — `sub_consultar`, `sub_link_pago`, `sub_pausar`, `sub_cancelar` y
   `sub_cambiar_plan` — definido en el core (`plugin_id="core"`) y **despachado al proveedor** del negocio. Se
   ofrecen solo cuando el tenant tiene habilitado un proveedor de suscripciones (predicado por request; no
   aparecen para los demás negocios). Las tools `sub_*` del pack se retiran.
4. **Estados y transiciones** (del diseño de SPEC-06): `trialing → activa` (pago de un plan) y `trialing →
   vencida` (día 30 sin pago); `activa ⇄ vencida` (cargo impago + gracia / pago del cargo); `pause → pausada →
   resume → estado previo`; `cancel → cancelada`. **Solo Free tiene prueba** (D-16): `trial_dias` solo lo aportan
   las variantes Free del catálogo.
5. **`charge_link` nunca crea un período futuro**: el período lo calcula el proveedor desde el calendario de la
   suscripción; una petición con `period` posterior al período actual pendiente se rechaza con `periodo_futuro`.
6. `change_plan` recibe el snapshot del plan nuevo; **la validación contra el catálogo de commerce es de
   HU-SUB-07** (cambio de plan e idempotencia por pedido); aquí el proveedor solo aplica el snapshot a la
   suscripción existente.
7. `pack_subscriptions` queda como **proveedor de referencia** del contrato (implementa las 8 operaciones sobre
   `pack_subscriptions_subscriptions`/`_charges`); el segundo proveedor (cobranza, préstamo rodante) se integra
   con el mismo contrato en HU-SUB-15. Los eventos de dominio (`domain_events`, outbox) son HU-SUB-03.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dejar las tools en cada proveedor (opción B de D-SPEC-06-1) | Dos juegos de tools para lo mismo; el LLM se confunde y el golden set diverge |
| Contrato solo de activación (estado previo) | Cada mejora de ciclo de vida se hace dos veces; el defecto 11 de la auditoría |
| Elegir proveedor por orden fijo | Un negocio con dos packs habilitados quedaría atado a un orden invisible; el error explícito lo hace visible en el panel |
| Mover las tools al core con validación de catálogo aquí | Duplica la responsabilidad de commerce (catálogo); la validación es de HU-SUB-07 |

## Consecuencias

**Positivas:** un comportamiento y un golden set; el negocio cambia de proveedor sin tocar su asistente; el LLM
nunca ve dos juegos de tools.

**Negativas / lo que aceptamos pagar:**
- Las tools `sub_*` cambian de nombre (de `sub_consultar_suscripcion` a `sub_consultar`, etc.): los golden sets
  y los evals que las citan deben actualizarse en esta HU.
- Hasta HU-SUB-15 solo hay un proveedor registrado; el camino de dos proveedores queda cubierto por tests del
  registro (ambigüedad y elección explícita).
- `change_plan` acepta el snapshot del caller sin validarlo contra el catálogo hasta HU-SUB-07.

**Qué haría que revisáramos esto:** HU-SUB-15 (segundo proveedor), HU-SUB-07 (cambio de plan con idempotencia) o
métricas de MRR/churn (HU-SUB-16).

## Impacto en el código

- `core/subscriptions/__init__.py`: protocolo v1 + `SubscriptionProviderAmbiguity` + `capability_for_tenant`.
- `core/subscriptions/tools.py` (nuevo): `SubscriptionSkill` del core con las 5 tools y predicado por proveedor
  habilitado; registrada en `register_core_skills`.
- `packages/pack-subscriptions/pack_subscriptions/capability.py` (nuevo): implementación de las 8 operaciones
  (lógica migrada de `skills.py`, que desaparece) + guard `periodo_futuro`.
- `packages/pack-subscriptions/pack_subscriptions/module.py`: registra la capacidad; sin `SubscriptionsSkill`;
  guardrail con los nombres nuevos.
- `packages/pack-subscriptions/pyproject.toml`: sin la tabla `pack_subscriptions_plans` (eliminada en `0002`) ni
  el campo `plan_id` (la columna es `variante_id`).
- `packages/pack-commerce/pack_commerce/module.py`: activación por `capability_for_tenant`.
- Tests: `tests/test_subscriptions_registry.py`, `tests/test_subscriptions_core_tools.py`; actualizados los que
  citaban las tools del pack.

## Enmienda 2026-10-23 · API de administración (SPEC-35, D-43)

La habilidad suma una API admin de **lectura y acciones** en `/api/v1/pack/pack_subscriptions`, para el panel
(entrada «Suscripciones»). No cambia el contrato `SubscriptionCapability`: las acciones reusan sus operaciones.

- **Lectura** (`require("pack_subscriptions:suscripciones")`, `Page[T]`): `GET /suscripciones` (filtros `estado`,
  `variante_id`, `periodicidad`, `con_mora`, `q`, `cliente_id`), `GET /suscripciones/{id}` (cargos, cambio
  programado, historial desde `audit_log` y acciones válidas por estado), `GET /cargos` (los «pagos» de la
  suscripción; filtros `estado`, `tipo`, `vencidos`, `desde`/`hasta`), `GET /clientes` y `GET /metricas`.
  Pagos = cargos: no se consulta `pack_payments` (los packs no se importan). Estados reales del cargo:
  `pendiente|pagado|anulado`.
- **Acciones** (`write=True`): `POST /suscripciones/{id}/pausar|reanudar|cancelar` y
  `POST /cargos/{id}/reenviar-link`. El panel solo ofrece las válidas para el estado; el servidor responde 409
  `transicion_invalida` si no lo son. Pausar, reanudar y cancelar escriben una fila de `audit_log` **en la misma
  transacción** (`actor = admin:<correo>`, `subs_pausada|subs_reanudada|subs_cancelada`); solo si hay `actor`,
  así las tools del LLM no duplican la auditoría que ya hace el host. `reanudar` vuelve siempre a `activa`.
- **Reenviar link:** reusa el link del cargo (o lo crea sin duplicar el cargo) y encola `subscription.charge_reminder`
  por el outbox con `reenvio` en el payload, cuya clave de aviso (`sub_resend:<cargo>:<sello>`) no se funde con el
  recordatorio del cron. Sin conversación de WhatsApp no se envía nada y se devuelve el link para copiarlo.
- **Clientes:** `pack_subscriptions` registra un `LeadProvider` (columnas `sub_*`, filtro `suscripcion:<estado|mora|ninguna>`
  y eventos de alta, cobro y baja en la línea de tiempo). Agente `suscripciones` en la ficha.
- **Datos:** migración `0008` con índices `(tenant_id, estado)` y `(tenant_id, estado, fecha_vencimiento)`.
- **Métricas:** solo `pack_subscriptions` (no cobranza, HU-SUB-15), una moneda (la de más suscripciones vivas), meses en
  UTC; MRR = activas de pago normalizadas por periodicidad; el MRR del mes anterior es aproximado (vivas entonces, a su
  precio de hoy).
