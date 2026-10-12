## Crons de seguimiento de pack-commerce: el negocio sin configurar ya no frena a los demás (2026-10-11)

- `pack_commerce.follow_up_quotes` y `pack_commerce.follow_up_leads` saltan el negocio que tiene el pack
  habilitado pero sin configurar, con un aviso `follow_up_sin_config` (nivel warning, con `tenant_id` y
  `kind`), y siguen con los demás. Antes el `PluginConfigError` de `load_config` cortaba el recorrido: el cron
  terminaba en `cron_task_failed` y los negocios siguientes se quedaban sin seguimiento (visto en el entorno de
  test, 4 veces en 7 h).
- Los crons de pack-calendar y pack-subscriptions ya caían a la config por defecto, y los de pack-cobranza no
  cargan config: no cambian.
