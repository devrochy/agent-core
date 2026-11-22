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

**El saldo de cada ciclo es el crédito mensual del plan más los abonos de ese ciclo. Los abonos se registran por ciclo en una tabla nueva, solo de inserción: al cerrar un ciclo, lo que queda de cada abono se vuelve a insertar para el ciclo siguiente. El consumo se sigue midiendo solo desde `usage_ledger`.**

1. **Tabla `credit_grants`** (negocio, RLS habilitado y forzado, policy de aislamiento):

   | Columna | Tipo | Nota |
   |---|---|---|
   | `id` | uuid | PK |
   | `tenant_id` | uuid not null | Índice `(tenant_id, ciclo)` |
   | `ciclo` | timestamptz not null | `inicio` exacto de la ventana del ciclo en que cuenta el abono (`ventana_mensual`, `core/billing/periodos.py:100`). No es el mes: un período reiniciado puede empezar dos ventanas en el mismo mes |
   | `tipo` | text | `acumulado` \| `recarga` (CHECK) |
   | `creditos` | numeric(12,1) | CHECK `> 0` |
   | `vence_en` | timestamptz null | Solo `acumulado`: fin del último ciclo en que puede contar |
   | `origen` | text not null | Id del pago de la recarga (`subscription_payments.id`) o ciclo donde nació lo acumulado |
   | `created_at` | timestamptz | |

   - **Única por `(tenant_id, ciclo, tipo, origen)`:** reintentar el cierre o el webhook no duplica.
   - **Solo de inserción,** como `usage_ledger` (ADR-0008): reutiliza el trigger `prevent_mutation()` (migración 0067) y da solo `SELECT, INSERT` a `app_runtime` (patrón de la 0069).
   - **No lleva `deleted_at`:** un abono no se borra. La única excepción es `purge_tenant` (invariante 8), que ya descubre las tablas por `tenant_id` y salta los triggers (migración 0072).
   - **El crédito del mes no se inserta:** sigue saliendo del plan.

2. **Saldo y orden de gasto.**
   - **Saldo del ciclo:** `credito_mensual + Σ creditos de los credit_grants con ciclo = el ciclo actual`. Una recarga comprada a mitad de ciclo se inserta con el ciclo actual y suma desde ese momento.
   - **Orden de gasto:** se gasta primero el mes, luego lo acumulado (primero el que vence antes) y por último las recargas.
   - **Cómo se calcula:** al leer (`medir_creditos`, `alcanza` en `core/billing/creditos.py`), sin columna de saldo ni contador (ADR-0045). Lo consumido sale siempre de `usage_ledger` en la ventana del ciclo. Con los grants del ciclo y el ledger, cualquier ciclo pasado se reconstruye sin saber qué plan tenía antes.

3. **Cierre del ciclo: arrastre y acumulación.** Al cerrar un ciclo, una tarea de `core/tasks/billing_lifecycle.py` reparte lo consumido según el orden del punto 2 y calcula lo que queda de cada fuente. Después inserta para el ciclo siguiente:
   - **Recargas:** lo que queda de cada recarga, con su mismo `origen`. No vencen.
   - **Acumulado que sigue vigente:** lo que queda de cada abono `acumulado` con `vence_en >= fin del ciclo siguiente`.
   - **Sobrante del mes:** como `acumulado` nuevo, con `origen = <inicio del ciclo que cierra>` y `vence_en` = fin del ciclo que cierra avanzado `features.acumulable_meses` períodos con `sumar_periodo` y el `dia_ancla` (no con un intervalo fijo de meses). Así Empresarial (2) conserva el sobrante de N en N+1 y N+2. Con `acumulable_meses` en 0 o ausente (Free y Basic), el sobrante del mes se pierde.
   - **Colchón:** lo consumido por encima del saldo (ADR-0208) no se arrastra como deuda: el ciclo siguiente empieza sin restar nada. La plataforma absorbe solo lo que quede descubierto al cierre. Si el negocio compra una recarga dentro del mismo ciclo, esa recarga cubre primero lo consumido en el colchón, porque el saldo del ciclo la incluye.
   - **Negocios vencidos o cancelados:** el cierre corre igual para todo negocio con grants, aunque `tenant_operativo` (`core/billing/enforcement.py:76`) lo tenga sin operar. Así las recargas no se pierden y vuelven a contar al reactivar. Lo acumulado sigue venciendo por su fecha.
   - **Período reiniciado:** cuando `aplicar_pago_de_periodo` reinicia `period_start` (`core/billing/service.py:284-288`, al volver tras cancelar o tras más de un período vencido), en la misma transacción se cierra la ventana que se abandona con este mismo arrastre, y los abonos pasan a la ventana nueva.
   - **Reintentos:** la tarea es idempotente por la unicidad del punto 1.

4. **Recargas: un pago más de la plataforma** (ADR-0123).
   - **Dónde quedan:** en `subscription_payments`, con una columna nueva `concepto` (`plan` \| `recarga`, por defecto `plan`) y `creditos` (null salvo en recargas). La recarga lleva `periodo = null` y `plan_id` = el plan vigente al comprar. `plan_id` es NOT NULL con FK (migración 0034) y deja auditable el descuento aplicado.
   - **Identificación:** la recarga se identifica por la fila del pago. `credit_grants.origen = subscription_payments.id`. El pago tiene el `provider_reference` del link de Wompi, único por negocio (migración 0034), como cualquier pago de la plataforma.
   - **Webhook:** `tenant_del_pago` (`core/billing/checkout.py:396`) resuelve el pago con `resolve_subscription_payment_tenant` (0034, sin cambios), así que el webhook (`core/gateway/webhooks.py:968`) **no** lo reenvía al negocio (`core/billing/enrutador.py`).
   - **Aplicación:** `apply_payment` (`checkout.py:239`) bifurca por `concepto` **justo después** de los chequeos de idempotencia y de monto (hoy líneas 288-306) y **antes** de la rama de `periodo` nulo (línea 315). Esa rama queda solo para pagos de plan antiguos. Una recarga aprobada inserta su `credit_grant` del ciclo actual y no toca el período, el plan ni el estado de la suscripción.
   - **Cambio de plan:** las anulaciones de pendientes al cambiar de plan (`core/billing/service.py:311-320`) y por período (`service.py:270-280`) se restringen a `concepto = 'plan'`. Un link de recarga pendiente sigue valiendo después de cambiar de plan.
   - **Proveedor:** `create_payment_link` (`core/billing/providers/base.py:47`, `wompi.py:71-91`) recibe el concepto y la descripción, porque hoy arma solo «Suscripción a SolversHub — plan X».
   - **Pedido:** `POST /admin/plan/recargas` en `core/api/routers/plan.py` recibe el paquete y arma el link con el precio de lista menos `features.descuento_recarga`.
   - **Quién no puede recargar:** un negocio sin suscripción activa o en Free recibe 409 `recarga_no_disponible`. Los negocios que pagan el plan por el asistente (`vinculo_asistente`, `checkout.py:126`) sí pueden recargar por el panel, y la conciliación del asistente no ve las recargas.
   - **Precios de los paquetes:** viven en el catálogo de `core/billing/plans.py`, junto a los planes. El mínimo del sandbox de Wompi (150.000, `core/payments/wompi_minimo.py`) impide pagar los paquetes en test.
   - **Lo que ya ignora las recargas:** la conciliación (`core/billing/conciliacion.py:185`), la renovación (`renovacion.py:120-128`) y `billing_lifecycle.py:81` filtran `periodo is not null`. Cualquier consulta nueva de ingresos del plan o MRR debe filtrar `concepto = 'plan'`.

5. **Lo que muestra `GET /admin/plan`.** El saldo desglosado en `mes`, `acumulado` (con su vencimiento más próximo) y `recargas`, y los paquetes con el precio del plan. El historial de pagos (`core/admin/plan_view.py:103-115`) suma `concepto` y `creditos` para que una recarga no se vea como pago del plan.

6. **Orden de las migraciones de SPEC-51:**
   1. ADR-0208: `'tts'` en `ck_usage_kind`.
   2. ADR-0209: `credit_grants` y las columnas de `subscription_payments`.
   3. ADR-0210: `agents.nivel_ia`.

   Las tres son aditivas.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Abonos como filas negativas en `usage_ledger` | Mezcla consumo con abono: rompe los tableros de costo y el `kind` del ledger significa gasto (ADR-0116/0117) |
| Columna `saldo` mutable en `tenant_subscriptions` | Es un contador paralelo (ADR-0045): se desincroniza con un reintento o una carrera y no se puede reconstruir |
| Recargas como tabla de pagos aparte | Exige un camino nuevo en el webhook y en el enrutador, y una segunda fuente del cobro de la plataforma (ADR-0123) |
| Acumular todo el sobrante sin vencimiento | El pasivo de créditos crece sin techo y desincentiva subir de plan. D-79 fija 1 o 2 meses |
| Saldo = mensual + todos los abonos vigentes, sin arrastre por ciclo | El consumo se mide desde cero en cada ciclo: una recarga parcialmente gastada volvería a sumar completa en el siguiente. Reconstruir lo que queda exigiría recorrer ciclos pasados con un `plan_id` que es mutable |
| Recargas con vencimiento corto (fin del ciclo siguiente) | Era la recomendación previa a D-79. El dueño eligió que no venzan mientras la suscripción siga activa |

## Consecuencias

**Positivas:**
- El saldo es reproducible: plan + abonos inmutables por ciclo + ledger inmutable dan el mismo número en cualquier momento.
- Las recargas entran por el mismo webhook, la misma conciliación y la misma tabla que el plan.
- La recarga da margen: entre 52 % y 65 % aun con descuento.

**Negativas / lo que aceptamos pagar:**
- `medir_creditos` suma una lectura de `credit_grants` por turno, filtrada por `(tenant_id, ciclo)`.
- El cierre de ciclo escribe unas filas por negocio y por abono vivo cada mes. Es poco volumen, pero la tarea debe correr para todos los negocios con abonos, incluidos los que no operan.
- Si la plataforma absorbe el colchón, una recarga comprada en el mismo ciclo lo paga primero. Es la regla más simple y queda dicha al dueño en el panel.
- El orden de gasto vive en código y hay que probarlo con cuidado: mes, acumulado y recarga, a mitad de ciclo y al cierre.
- Las recargas sin usar son un pasivo que no vence mientras la suscripción siga activa.
- **Una recarga no se puede reembolsar parcialmente:** un grant no se edita. Un reembolso pediría un grant negativo, que esta decisión no admite.

**Qué haría que revisáramos esto:**
- Que más del 30 % de los créditos de recarga sigan sin usar a los 12 meses.
- Que haga falta reembolsar una recarga.
- Que un negocio con recargas cambie de plan y el orden de gasto produzca un saldo que el dueño no entienda (reportado en soporte).

## Impacto en el código

- **Migración del core:**
  - tabla `credit_grants` con RLS forzada, policy, índice `(tenant_id, ciclo)`, `prevent_mutation()` y permisos `SELECT, INSERT`;
  - columnas `concepto` y `creditos` en `subscription_payments` (aditiva, con `concepto` por defecto `plan`).
  - Pasa por la skill `db-migration` y por `auditor-multitenant`.
- **`core/billing/`:**
  - `creditos.py`: saldo del ciclo y orden de gasto;
  - `checkout.py`: link de recarga y bifurcación en `apply_payment` antes de la línea 315;
  - `service.py`: anulaciones solo de `concepto = 'plan'`;
  - `providers/base.py` y `providers/wompi.py`: concepto y descripción del link;
  - `plans.py`: paquetes y features `acumulable_meses` y `descuento_recarga`.
- **`core/tasks/billing_lifecycle.py`:** cierre de ciclo con arrastre y acumulación, para todo negocio con abonos.
- **API:** `core/api/routers/plan.py` y `core/admin/plan_view.py` (saldo desglosado, `POST /recargas`, `concepto` en el historial de pagos), y `docs/openapi.json`.
- **La función `resolve_subscription_payment_tenant` no cambia.**
