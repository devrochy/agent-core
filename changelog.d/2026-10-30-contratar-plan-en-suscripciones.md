## Contratar un plan desde suscripciones (2026-10-30)

- SPEC-43 · HU-SUB-29 y lado del agente de HU-SUB-34 (PR-3). Nuevo procedimiento `pack_subscriptions.contratar_plan` (v2.0.0, pack `pack_subscriptions` 0.4.0) con las tools del core `sub_listar_planes`, `sub_cotizar_inscripcion`, `sub_confirmar_inscripcion` y `sub_inscribir`, que despachan a la capability de inscripción. El modelo solo manda el código del plan, el código de promoción, los datos y el monto que el cliente confirmó; el cliente sale del contexto y el precio, la periodicidad y el descuento los calcula el servidor (el monto se compara, nunca se usa). Solo las ofrece el motor de procedimientos.
- El botón de confirmación muestra el resumen del servidor (plan, descuento, total del primer cargo, datos y términos). `register_resumen` acepta tools del core, no solo las del propio pack.
- `sub_cotizar_cambio_plan` y `sub_aplicar_cambio_plan` aceptan `codigo_promocion`; el código viaja al proveedor solo si el cliente lo trajo. `cotizar_inscripcion` recibe el `customer_id` para el límite por cliente.
- El verificador de montos conoce `monto_a_pagar` y `precio_nuevo` como dinero.
- La plantilla «Suscripciones» incluye el procedimiento; los playbooks `planes` y `suscripciones` dejan de decir que el plan es un producto de commerce. Los escenarios de QA apuntan al id nuevo.
- Queda igual hasta el PR-4: el procedimiento `pack_commerce.contratar_plan`, la plantilla «Ventas» y su pregunta `que_vendes` (panel y APK la envían; se quitan juntos), y los datos de prueba de planes en commerce.
