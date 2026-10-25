## Aviso de cobro con datos de transferencia cuando el cargo no tiene link (HU-SUB-24, adenda de ADR-0067)

Un cargo sin link (monto bajo el mínimo de la pasarela, negocio sin pasarela o link caído) avisaba al cliente «págalo aquí:» con el link vacío; el fallo solo quedaba en el log.

- `core.payments.transferencia`: las cuentas de transferencia son una capacidad del core (`TransferDetailsProvider`, registro por pack habilitado, `formatear_cuentas`). `pack_commerce` registra las suyas (`transfer_accounts`) y suscripciones las consulta sin importarlo. El chat de ventas imprime el mismo bloque.
- Avisos de cobro de `pack_subscriptions` (cargo nuevo, recordatorio, pausa por mora): sin link llevan las cuentas, con textos de respaldo propios (`aviso_cargo_transferencia_texto`, `recordatorio_transferencia_texto`, `aviso_pausa_transferencia_texto`).
- Variables nuevas para plantillas de Meta: `cargo.datos_transferencia` y `cargo.forma_de_pago` (link o cuentas). Una plantilla que mapea `cargo.link` falla en un cargo sin link: hay que pasarla a `cargo.forma_de_pago` (`docs/plantillas-suscripciones.md`).
- Sin link y sin cuentas no sale el aviso al cliente: el cargo queda en Pagos sin link, se audita (`subs_cargo_sin_forma_de_pago`) y el dueño recibe una notificación. Cambia el comportamiento de negocios sin pasarela ni cuentas, que antes mandaban el aviso con el link vacío.
- Sin migración ni cambio de playbook (no hay que regrabar cassettes; el gate de evals en replay pasa).
