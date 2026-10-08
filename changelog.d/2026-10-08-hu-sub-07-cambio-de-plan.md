## Cambio de plan con precio del servidor, prorrateo y downgrade programado (2026-10-08)

- HU-SUB-07 (ADR-0111). `sub_cambiar_plan` solo recibe el SKU del plan nuevo: precio, moneda y periodicidad salen
  del catálogo del negocio (`plan_catalog_registry`, que commerce registra). Antes el LLM pasaba el precio y un
  journey vio un plan quedar en $0.
- Upgrade: cargo `cambio_plan` por la diferencia prorrateada por días (desde Free, el precio completo y un período
  nuevo), con link de pago en línea o pendiente para transferencia; el plan cambia al confirmarse el pago.
  Downgrade: programado para el próximo período, sin reembolso (`generar_cargos` lo aplica). Migración
  `pack_subscriptions_0006` (`tipo`/`cambio_plan` en cargos, `referencia_origen`/`cambio_programado` en suscripciones).
- La suscripción guarda la referencia del pedido (índice único por negocio): el mismo pedido dos veces no crea dos
  suscripciones. `SubscriptionConfig.varias_por_cliente` (apagado por defecto): si está apagado, commerce no crea un
  pedido de otro plan pago para un cliente con suscripción paga y le indica al asistente que ofrezca el cambio.
- Los jobs de período y `charge_link` ignoran los cargos de cambio.
- `crm_amount_check` reconoce `monto`, `valor_periodo` y `amount` como montos verificados (antes bloqueaba y
  escalaba al mencionar el valor de una suscripción o la diferencia de un cambio de plan).
