# ADR-0177 — Los planes y la inscripción viven en suscripciones; comercio vende solo productos

- **Estado:** propuesto (se acepta cuando el tren de PR de SPEC-43 llegue a `develop` y se despliegue junto)
- **Fecha:** 2026-10-28
- **Contexto:** SPEC-43 / D-55 (docs), REQ-SUB-10, REQ-SUB-11, REQ-COM-09.
- **Usa:** ADR-0057 (identidad de pack), ADR-0097 (el core no importa de los packs; patrón de registry), la invariante n.º 5 (un pack no importa ni lee tablas de otro), ADR-0121/0123 (alta automática y cobro de plataforma) y ADR-0111 (cambio de plan).
- **Supersede:** ADR-0083 (planes como productos de commerce) y ADR-0106 (contrato `SubscriptionCapability` v1).
- **Enmienda:** ADR-0111 (el cambio de plan resuelve el plan en el mismo proveedor), ADR-0121 (la fuente de altas pasa a la inscripción) y ADR-0123 (`plan_plataforma` reemplaza a `precio_de_plan`).

## Contexto

Hoy el plan es una variante de commerce: commerce activa la suscripción cuando se paga el pedido y el alta
automática lee el pedido. `pack_subscriptions` no sabe qué vende y un segundo proveedor (cobranza) no puede
serlo sin leer tablas ajenas.

## Decisión

1. **El plan es de suscripciones.** Tabla `pack_subscriptions_plans` (código único por negocio entre los no
   borrados, precio, moneda, periodicidad, `trial_dias`, `entitlements`, `plan_plataforma`, `activo`, `orden`,
   borrado lógico, RLS forzada). El CHECK `trial_dias = 0 OR precio = 0` pone D-16 en el esquema.
   `entitlements` es un objeto jsonb (`{"clientes": 500}`), no una lista; `PlanSnapshot.entitlements` pasa de
   `list[str]` (tipo muerto) a `dict[str, Any]`. `core/billing` no lo lee: usa `features` y `limits` de
   `core/billing/plans.py`.
2. **La inscripción es de suscripciones.** Un plan de pago nace `pendiente_pago` con su primer
   `SubscriptionCharge` de tipo `inscripcion` y pasa a `activa` con `payment.confirmed` de `sub-<cargo>` o con la
   confirmación manual del panel. Un plan de $0 nace `trialing` (con prueba) o `activa`, sin cargo. Un cliente
   con Free (vigente o vencida) que contrata un plan de pago hace un **cambio de plan `desde_cero`** sobre la
   misma suscripción (no nace una segunda ni un segundo negocio en el alta automática). `numero_publico`
   (`SUB-0001`, contador por negocio) reemplaza a `referencia_origen` como referencia pública.
3. **`pendiente_pago` no es una suscripción viva.** Queda fuera de `VIVAS`, del MRR, de las altas, del churn, de
   las cohortes, de la mora y de los crons de cobro. Un CHECK impide que tenga período (`proximo_cargo` y
   `periodo_actual_inicio` nulos). Solo se puede cancelar. Un pago que llega a una inscripción ya cancelada,
   expirada o reemplazada NO la activa: el cargo queda `anulado`/`pagado` según el caso y se avisa al negocio
   (`subs_pago_sin_aplicar`) para resolverlo a mano.
4. **Un solo camino de activación.** `inscripcion._activar_por_pago` lo usan el webhook y la confirmación manual.
   Orden de locks: advisory (negocio, cliente) → suscripción → cargo. El link de pago se crea después del commit
   (la inscripción existe aunque la pasarela falle) y se reintenta con `charge_link`. Repetir una inscripción
   devuelve la misma (mismo cargo y link) sin gastar cupo del antiabuso.
5. **Expiración.** El cron `expirar_inscripciones` cancela las `pendiente_pago` con más de
   `dias_pago_inscripcion` (7 por defecto) con motivo `pago_no_recibido` y anula su cargo; cada una en su
   transacción, releyendo el estado bajo lock (si el pago llegó a la vez, gana quien toma el lock primero).
6. **Contrato v2, aditivo en este PR.** `InscripcionCapability` (Protocol aparte, operaciones opcionales):
   `listar_planes`, `plan_por_codigo`, `cotizar_inscripcion`, `inscribir`. `activate` y `plan_catalog_registry`
   siguen vivos hasta el PR de commerce sin planes. `SubscriptionView` suma `numero_publico`, `plan_id`,
   `cargo_inscripcion` e `inscripcion`; `SubscriptionError` suma `data` y `escalate`.
7. **La fuente de altas es la inscripción.** `pack_subscriptions/altas.py` registra una `FuenteDeAltas` junto a la de
   commerce: el core pregunta a cada fuente del negocio hasta que una conoce la referencia
   (`fuentes_de_alta.del_negocio`, `plan_plataforma_de`). `subscription.activated` lleva `plan_id`,
   `numero_publico` y la bandera `cambio_de_plan`; el consumer decide el cambio por la bandera (no por la
   referencia, que es la misma entre el alta y el cambio) y manda a revisión humana el alta con
   `origen = 'panel'` (datos tecleados por un operador sin los topes del chat), salvo que ya tenga aprobación.
   La clave de idempotencia distingue el alta (`sub_activated:{sub}:alta`) del cambio
   (`sub_activated:{sub}:cambio:{cargo}`): el cambio A → B → A ya no choca.
8. **Antiabuso y términos migran a la inscripción** y solo se evalúan con `origen = 'agente'`. El LLM manda
   `plan_code`, datos y el token de términos; precio, periodicidad y cliente los fija el servidor.
9. **Siembra y precios.** `planes.json` → `seed.py` (upsert por `(negocio, código)`, todo o nada); con
   `plan_plataforma` el precio sale de `core/billing/plans.py` y el cron `sincronizar_precios_de_plan` lo mantiene
   (un plan con prueba no sube de precio: el CHECK lo rechazaría, se avisa). El seed genérico del core lee también
   los archivos que un pack declare en `archivos_de_seed`.

## Migración `pack_subscriptions_0010` (irreversible para los datos)

Aditiva en el esquema: `plan_id` y `numero_publico` quedan **NULLABLE** (los NOT NULL llegan con `0013`, en el
PR que quita los planes de commerce), para que `activate` y el camino viejo sigan funcionando. **Borra todas las
filas de suscripciones y cargos** porque son datos de test descartables por decisión del dueño (D-55); el
downgrade revierte solo el esquema. Runbook con precheck de conteo: `docs/runbooks/planes-e-inscripcion-despliegue.md`.
**Nunca contra una base que no sea de test o la efímera local.**

## Fuera de este PR

Promociones y descuento de N períodos (PR-2b), tools `sub_*` y procedimiento `contratar_plan` (PR-3), quitar los
planes de commerce y la contracción `0013` (PR-4), panel, APK y comercial.

## Consecuencias

- **Positivas.** Un dueño por concepto; D-16 en el esquema; se puede expirar, recordar y medir la espera de pago;
  un segundo proveedor es posible sin romper la invariante n.º 5.
- **Negativas.** Migración con borrado de datos de test; un estado más en todas las consultas de suscripciones;
  dos caminos conviven hasta el PR de commerce sin planes.
- **Observabilidad.** Logs `subs_inscripcion_creada|activada|expirada|reemplazada`, `subs_inscripcion_pago_manual`,
  `subs_inscripcion_link_fallo`, `precios_de_plan_*`; auditoría `subs_plan_*`, `subs_pago_inscripcion_confirmado`,
  `subs_pago_sin_aplicar`. Sin correo, teléfono ni documento en los logs.

## Verificación

`tests/integration/test_subscription_inscripcion.py` (concurrencia, idempotencia, activación, expiración, métricas,
aislamiento y FK compuesta) y `tests/integration/test_subscription_planes_api.py` (CRUD, API, confirmación manual,
fuente de altas, siembra y precios).
