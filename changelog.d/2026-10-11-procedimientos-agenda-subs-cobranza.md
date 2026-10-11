## Agent OS: procedimientos de agenda, cambio de plan y promesa de pago (2026-10-11)

- HU-AOS-08 (#369). Tres procedimientos `procedure/1` publicados por sus packs, detrás de
  `tenants.aos_procedures` (apagada por defecto: sin cambio de conducta ni de lo que ve el modelo).
  - `pack_calendar.agendar_cita`: el resolver `horario_ofrecido` rechaza un horario que la agenda del
    servidor no ofrece y devuelve los libres del día; la cita se crea después del botón.
  - `pack_subscriptions.cambiar_plan`: precio y prorrateo del catálogo del servidor (ADR-0111) con las
    tools del core `sub_cotizar_cambio_plan` y `sub_aplicar_cambio_plan` (solo las ofrece el motor). El
    botón muestra el monto del servidor y `change_plan` lo compara con su cálculo en la misma transacción
    (`monto_esperado`). Con el motor activo, `sub_cambiar_plan` no se ofrece.
  - `pack_cobranza.promesa_de_pago`: la fecha la validan las reglas del pack (prórroga máxima, domingo no
    laborable, promesas pendientes) y la promesa queda por el saldo real (`cob_registrar_promesa_saldo`).
- `core/kernel/procedures/de_pack.py`: carga los JSON de `packages/pack-*/procedures/`.
- `core/kernel/oferta.py`: con `aos_procedures` encendida no se ofrecen `sub_cambiar_plan` ni
  `cob_registrar_promesa_pago` (irían sin botón); las acciones nuevas solo se ofrecen con la bandera.
- `pack_subscriptions.capability.evaluar_cambio`: las reglas del cambio de plan en una función pura (sin
  cambio de conducta de `change_plan`).
- Adendas a ADR-0111 y ADR-0113. Sin migración.
