## Promesa de pago por lo vencido o por el saldo total (2026-10-11)

- **Decisión del dueño (HU-AOS-08):** el procedimiento `pack_cobranza.promesa_de_pago` (2.0.0) ofrece
  prometer por lo vencido o por el saldo total, con los montos calculados por el servidor. Si lo vencido
  es 0 o igual al saldo, hay una sola opción y queda elegida.
- Tool nueva solo de procedimiento `cob_opciones_promesa(eleccion?)`; `cob_registrar_promesa_saldo` pasa a
  `(fecha, alcance, monto)`: los tres ligados a los datos de la tarea, y un monto distinto del servidor no
  se registra. El botón muestra el alcance y el monto del servidor (resumen del pack).
- Regla por negocio `alcance_promesa` (`ambos` por defecto, `vencido` o `saldo_total`) en las reglas de
  negociación (`GET/PUT /api/v1/pack/cobranza/rules`; omitirla en el PUT la conserva). Las promesas guardan
  su `alcance` (sale en `GET /promises`). Migración del pack `pack_cobranza_0017`.
- pack-cobranza 0.6.0. Con `tenants.aos_procedures` apagada no cambia nada: `cob_registrar_promesa_pago`
  sigue igual y las tools nuevas no se ofrecen. Adenda HU-AOS-08 de ADR-0113 actualizada.
