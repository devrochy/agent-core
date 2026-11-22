# ADR-0209 — Saldo de créditos: crédito del mes, acumulado y recargas

- **Estado:** propuesto (HU-BILL-17, 2026-11-22).
- **Fecha:** 2026-11-22
- **Contexto:** D-79 y SPEC-51 (`solvershub-docs`, #291), HU-BILL-22 y HU-BILL-23. P-07 cerrada por D-79.
  **Enmienda:** ADR-0144 §8 (el límite deja de ser solo `credito_mensual`).
  **Usa:** ADR-0008 (solo inserción), ADR-0045 (sin contadores mutables), ADR-0123 (una sola fuente del
  cobro de la plataforma y la referencia del período), ADR-0208 (el turno consulta el saldo).

## Contexto

Hoy el límite de créditos es solo `features.credito_mensual` del plan (`core/billing/plans.py:179`). Lo usado se mide desde cero en cada ciclo (`ventana_mensual`, `core/billing/periodos.py:100`) y lo que sobra se pierde.

D-79 pide tres cosas:
- recargas compradas por Wompi, con descuento según el plan y sin vencer mientras la suscripción siga activa;
- créditos que pasan al ciclo siguiente en Premium (1 mes) y Empresarial (2 meses);
- un saldo único que el turno pueda consultar (ADR-0208).

## Decisión

**El saldo del ciclo es el crédito mensual del plan más los abonos vigentes del negocio. Los abonos se registran en una tabla nueva, solo de inserción, y el consumo se sigue midiendo solo desde `usage_ledger`.**

1. **Tabla `credit_grants`** (negocio, RLS habilitado y forzado, policy de aislamiento):

   | Columna | Tipo | Nota |
   |---|---|---|
   | `id` | uuid | PK |
   | `tenant_id` | uuid not null | Índice `(tenant_id, vence_en)` |
   | `tipo` | text | `acumulado` \| `recarga` (CHECK) |
   | `creditos` | numeric(12,1) | CHECK `> 0` |
   | `vence_en` | timestamptz null | `null` = no vence mientras la suscripción siga activa |
   | `referencia` | text not null | Ciclo de origen (`acumulado`) o referencia del pago (`recarga`) |
   | `created_at` | timestamptz | |

   - Única por `(tenant_id, tipo, referencia)`: reintentar el cron o el webhook no duplica.
   - Es solo de inserción, como `usage_ledger` (ADR-0008): un trigger rechaza UPDATE y DELETE.
   - No lleva `deleted_at`: un abono no se borra. La única excepción es `purge_tenant` (invariante 8).
   - El crédito del mes **no** se inserta: sigue saliendo del plan.

2. **Saldo y orden de gasto.**
   - **Saldo del ciclo:** `credito_mensual + Σ creditos de credit_grants vigentes al inicio del ciclo o creados dentro de él`.
   - **Orden de gasto:** se gasta primero el mes, luego lo acumulado (primero el que vence antes) y por último las recargas.
   - **Cómo se calcula:** es un cálculo al leer (`medir_creditos`, `alcanza` en `core/billing/creditos.py`), sin columna de saldo ni contador (ADR-0045). Lo consumido sale siempre de `usage_ledger`.
   - **Qué se lleva cada abono:** el orden decide qué abono se consume y, por lo tanto, cuánto queda de cada uno para el cierre del ciclo.

3. **Acumulación.**
   - **Cuándo:** al cerrar un ciclo, una tarea en `core/tasks/billing_lifecycle.py` calcula el sobrante del crédito del mes de ese ciclo.
   - **Qué se acumula:** solo el del mes, nunca el de un abono. Lo inserta como `acumulado` con `referencia = <AAAAMM del ciclo>` y `vence_en = fin del ciclo + features.acumulable_meses`.
   - **Planes sin la función:** con `acumulable_meses` en 0 o ausente, no se inserta nada.
   - **Abonos que quedan:** lo acumulado que sobra al vencer simplemente deja de contar. Las recargas siguen hasta agotarse.

4. **Recargas: un pago más de la plataforma** (ADR-0123).
   - **Dónde quedan:** se registran en `subscription_payments` con una columna nueva `concepto` (`plan` \| `recarga`, por defecto `plan`) y `creditos` (null salvo en recargas), con `periodo = null`. Así no tocan el índice único de un pago aprobado por período.
   - **Referencia:** `rec-<tenant8>-<hex>`, con la forma del `sub-<tenant8>-<hex>` de `start_subscription` (`core/billing/checkout.py:204`). No puede empezar por `plat-`, porque `periodos.py:23` toma `^plat-.+-AAAAMM$` como período del plan.
   - **Webhook:** como el pago está en `subscription_payments`, `tenant_del_pago` (`checkout.py:396`) lo resuelve y el webhook (`core/gateway/webhooks.py:968`) **no** lo reenvía al negocio (`core/billing/enrutador.py`).
   - **Aplicación:** `apply_payment` (`checkout.py:239`) bifurca por `concepto`. Una recarga aprobada inserta el `credit_grant` de tipo `recarga` con la referencia del pago, y no toca el período, el plan ni el estado de la suscripción.
   - **Pedido:** `POST /admin/plan/recargas` en `core/api/routers/plan.py` recibe el paquete y arma el link con el precio de lista menos `features.descuento_recarga`.
   - **Planes que no pueden recargar:** sin suscripción activa o en Free, responde 409 `recarga_no_disponible`.
   - **Precios de los paquetes:** viven en el catálogo de `core/billing/plans.py`, junto a los planes, no en el panel.

5. **Vigencia de las recargas.**
   - **Mientras la suscripción esté activa:** no vencen (`vence_en = null`).
   - **Si se vence o se cancela:** dejan de contar mientras tanto y vuelven a contar si se reactiva.
   - **Si se purga el negocio:** se borran con él.

6. **Lo que muestra `GET /admin/plan`.** El saldo desglosado en `mes`, `acumulado` (con su vencimiento más próximo) y `recargas`, más los paquetes con el precio del plan.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Abonos como filas negativas en `usage_ledger` | Mezcla consumo con abono: rompe los tableros de costo y el `kind` del ledger significa gasto (ADR-0116/0117) |
| Columna `saldo` mutable en `tenant_subscriptions` | Es un contador paralelo (ADR-0045): se desincroniza con un reintento o una carrera y no se puede reconstruir |
| Recargas como tabla de pagos aparte | Exige un camino nuevo en el webhook y en el enrutador, y una segunda fuente del cobro de la plataforma (ADR-0123) |
| Acumular todo el sobrante sin vencimiento | El pasivo de créditos crece sin techo y desincentiva subir de plan. D-79 fija 1 o 2 meses |
| Recargas con vencimiento corto (fin del ciclo siguiente) | Era la recomendación previa a D-79. El dueño eligió que no venzan mientras la suscripción siga activa |

## Consecuencias

**Positivas:**
- El saldo es reproducible: plan + abonos inmutables + ledger inmutable dan el mismo número en cualquier momento.
- Las recargas entran por el mismo webhook, la misma conciliación y la misma tabla que el plan.
- La recarga da margen: entre 52 % y 65 % aun con descuento.

**Negativas / lo que aceptamos pagar:**
- `medir_creditos` suma una lectura de `credit_grants` por turno; es pequeña e indexada por `tenant_id`.
- El orden de gasto vive en código y hay que probarlo con cuidado: mes, acumulado y recarga, a mitad de ciclo y al cierre.
- Las recargas sin usar son un pasivo que no vence mientras la suscripción siga activa.
- **Una recarga no se puede reembolsar parcialmente:** un grant no se edita. Un reembolso pediría un grant negativo, que esta decisión no admite.

**Qué haría que revisáramos esto:**
- Que más del 30 % de los créditos de recarga sigan sin usar a los 12 meses.
- Que haga falta reembolsar una recarga.
- Que un negocio con recargas cambie de plan y el orden de gasto produzca un saldo que el dueño no entienda (reportado en soporte).

## Impacto en el código

- **Migración del core:** tabla `credit_grants` con RLS forzada, policy, índice que empieza por `tenant_id` y trigger de solo inserción. Además, las columnas `concepto` y `creditos` en `subscription_payments` (aditiva, con `concepto` por defecto `plan`). Pasa por la skill `db-migration` y por `auditor-multitenant`.
- **`core/billing/`:**
  - `creditos.py`: saldo y orden de gasto;
  - `checkout.py`: link de recarga y bifurcación en `apply_payment`;
  - `plans.py`: paquetes y features `acumulable_meses` y `descuento_recarga`.
- **`core/tasks/billing_lifecycle.py`:** acumulación al cierre.
- **API:** `core/api/routers/plan.py` y `core/admin/plan_view.py` (saldo desglosado, `POST /recargas`), y `docs/openapi.json`.
- **La función `resolve_subscription_payment_tenant` no cambia:** ya resuelve por `provider_reference` de `subscription_payments`.
