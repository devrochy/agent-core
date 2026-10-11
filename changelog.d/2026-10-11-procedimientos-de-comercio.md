## Procedimientos de comercio: contratar un plan y pedido con entrega (2026-10-11)

- pack-commerce 2.1.0 publica `pack_commerce.contratar_plan` y `pack_commerce.tomar_pedido` como datos (`procedures/*.json`), con resolvers (plan, método de pago, tipo y nombre del negocio), la tool `cotizar_plan` (solo con el motor activo) y el resumen de `confirmar_cotizacion` leído de la cotización vigente (HU-AOS-07).
- Motor de procedimientos: argumentos ligados a los datos de la tarea, invalidación y retroceso de etapa cuando un dato cambia, datos congelados tras el efecto en una etapa de riesgo alto y resumen de confirmación provisto por el pack (`MCPHost.register_resumen`).
- El manifiesto de la versión de un pack incluye sus procedimientos publicados (con el cargador `de_pack`): suben pack-calendar 2.2.0, pack-subscriptions 0.2.0 y pack-cobranza 0.5.0.
- Con el motor activo, las frases (`claims`) de los procedimientos disponibles se verifican.
- Journeys: `motor_de_procedimientos` en `negocio.json` y set `procedimientos` (sin grabación; listo para medir pass^4 con OK del dueño).
- Todo solo con `tenants.aos_procedures`; con la bandera apagada no cambia nada de lo que ve el modelo.
