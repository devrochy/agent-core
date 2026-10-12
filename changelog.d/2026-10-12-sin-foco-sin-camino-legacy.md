## Agent OS: sin tarea en foco no hay camino de siempre para lo que se confirma (2026-10-12)

- Arreglo del hallazgo de la prueba real en test. Con `tenants.aos_procedures` encendida y sin tarea en foco,
  el modelo cerraba una venta con `confirmar_cotizacion` → `crear_pedido` sin iniciar el procedimiento.
- **Las puertas de confirmación no se ofrecen sin foco.** Son las de los procedimientos disponibles del
  negocio (`motor.acciones_de_puerta`): hoy `confirmar_cotizacion`, `create_calendar_event`,
  `cob_registrar_promesa_saldo` y `sub_aplicar_cambio_plan`. Si el modelo igual las llama, se niegan
  (`audit_log denied`) con un texto que nombra el procedimiento a iniciar con `iniciar_procedimiento`.
- **Lo que sigue ofrecido sin foco:**
  - las consultas;
  - `crear_pedido`, que exige una cotización aceptada;
  - `confirmar_pago_pedido`, para reenviar el link de un pedido que ya existe.
- **Pista en la tarjeta de estado.** Con el motor activo, sin foco y con procedimientos que iniciar, la
  tarjeta lleva la pista de iniciar el recorrido. Sale del playbook del kernel `estado-conversacion`, no del
  negocio. La descripción de `iniciar_procedimiento` también se reforzó.
- **Respaldo v2:** con una tarea activa que el motor no gobierna, las puertas de su procedimiento no se ocultan
  ni se niegan. La decisión se relee siempre bajo el lock.
- **Endurecimiento con foco:** una puerta del core que el turno ya ofrecía, como `sub_aplicar_cambio_plan`, no
  pasa por el atajo de las acciones del core. Fuera de su etapa se niega.
- **Bandera apagada o modo sombra:** sin cambio, y el gate en replay pasa sin regrabar. Sin migración.
- Adenda "sin foco no hay camino de siempre" en ADR-0113.
