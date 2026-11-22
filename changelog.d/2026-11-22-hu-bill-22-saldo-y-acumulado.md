## Saldo con crédito del mes, acumulado y recargas (2026-11-22, HU-BILL-22)

- **Tabla nueva `credit_grants`** (migración 0155, después de la 0153 y la 0154; ADR-0209 §1 y §6). Guarda los abonos de cada ciclo: `ciclo` es el inicio exacto de la ventana de `ventana_mensual`, `tipo` es `acumulado` (con `vence_en`) o `recarga` (sin vencimiento) y `origen` es el pago de la recarga o el ciclo donde nació lo acumulado. Es única por `(tenant_id, ciclo, tipo, origen)`. Tiene RLS habilitada y forzada con la policy de aislamiento. Es solo de inserción: `prevent_mutation()` rechaza UPDATE, DELETE y TRUNCATE, y `app_runtime` solo tiene SELECT e INSERT. No lleva `deleted_at`, y `purge_tenant` la borra como al resto de las tablas con `tenant_id`.
- **Saldo del ciclo (§2):** el límite de `alcanza` y `decidir_turno` pasa a ser `monthly_credits` más los `credit_grants` del ciclo actual. Lo consumido se sigue midiendo solo del ledger y se reparte con `creditos.repartir`, en este orden: primero el mes, después lo acumulado (el que vence antes primero) y al final las recargas. `SaldoDeCreditos.desglose` trae el reparto. No hay contador ni columna de saldo (ADR-0045).
- **Colchón:** sigue siendo el 5 % del crédito mensual del plan (ADR-0208 §5). Lo usado del colchón se mide contra el saldo total, así que una recarga del mismo ciclo cubre primero lo consumido en él.
- **Cierre del ciclo (§3):** el cron nuevo `core.billing_cierre_ciclo` corre cada hora (minuto 5) y cierra el ciclo que terminó en los últimos 7 días. Para el ciclo siguiente inserta:
  - lo que queda de cada recarga;
  - lo que queda de lo acumulado que sigue vigente;
  - el sobrante del mes como acumulado, en Premium (vence en 1 período) y Empresarial (en 2), con `vence_en` calculado por calendario con `sumar_periodo`.

  Free, Basic y las suscripciones sin período pago no acumulan el sobrante. El cierre corre también para negocios vencidos o cancelados, así sus recargas no se pierden. Es idempotente (`ON CONFLICT DO NOTHING`). Lo consumido en el colchón no se arrastra como deuda.
- **Período reiniciado:** cuando `aplicar_pago_de_periodo` mueve `period_start` a una ventana distinta, la ventana abandonada se cierra en la misma transacción, con el mismo arrastre.
- **`/admin/plan` (cambio de contrato, aditivo):** `credit` suma `balance` (`CreditBalance`), con `total`, `month`, `accumulated`, `accumulated_expires_at`, `top_ups`, `used_month`, `used_accumulated`, `used_top_ups` y `over_balance`, y también `balance_percent`. `limit` y `percent` siguen siendo del crédito mensual.
- **El saldo puede llevar décimas:** el 409 `plan_limit_creditos` y la cotización de fotos informan el límite entero hacia abajo.
