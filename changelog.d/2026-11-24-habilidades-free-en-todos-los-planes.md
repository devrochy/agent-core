## Todas las habilidades y tipos de agente son Free (2026-11-24)

- `min_plan = "free"` en `pack_commerce`, `pack_subscriptions`, `pack_cobranza` y `pack_payments`, y `plan_minimo = "free"` en los presets: Ventas, Agenda, Suscripciones y Cobranza se crean en cualquier plan, y los superpoderes (Pagos en línea, Voz, Conocimiento) también.
- Quedan igual los topes por plan (agentes 1/2/5/sin tope, conexiones, clientes, créditos, canales, nivel de IA…). El candado por habilidad (409 `plan_plugin_not_included`) sigue vivo para limitar superpoderes más adelante; solo cambia el `min_plan` de cada pack.
