## Créditos de regalo: abono `bonificacion` (2026-11-27)

- HU-FB-02 (SPEC-54, ADR-0213 §1-3 y §10): `credit_grants` admite un tercer tipo de abono,
  `bonificacion`, para los créditos que SolversHub regala cuando aprueba un reporte de un cliente.
  No vence (`vence_en` null, como la recarga).
- Migración `0161`: recrea `ck_credit_grants_tipo` y `ck_credit_grants_vence_en` con `NOT VALID` y
  `VALIDATE CONSTRAINT`, con `lock_timeout` de 5 s. Aditiva. El downgrade falla con un mensaje claro si ya
  hay bonificaciones: en ese caso basta con volver el código.
- Nuevo orden de gasto: mes → acumulado (el que vence antes primero) → bonificaciones → recargas. Lo
  regalado se gasta antes que lo pagado. El saldo del turno (`_saldo_del_mes`) y `/admin/plan` suman las
  bonificaciones al total.
- El cierre del ciclo arrastra lo que queda de cada bonificación con su mismo `origen`, igual que la
  recarga, también con la suscripción cancelada o vencida.
- `GET /api/v1/admin/plan`: `CreditBalance` suma `bonuses` y `used_bonuses` («Regalos» en el panel),
  que entran en `total` y en `balance_percent`. Aditivo: los dos campos traen 0 por defecto.
- `core/billing/bonificacion.py::acreditar_bonificacion` inserta el abono en el ciclo actual, una sola vez
  por `origen` (`acreditada`, `ya_acreditada` o `sin_ciclo`). Todavía no la llama nadie: la aprobación de
  reportes llega en HU-FB-04.
