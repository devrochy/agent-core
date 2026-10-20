## Smoke del dueño en test: un solo resumen, correos mal escritos, lead y pedido de plan (2026-10-20)

- Smoke del dueño en test (2026-10-20, chat web de `solvershub`), adenda 2026-10-20 de ADR-0113:
  - Contratar un plan pedía confirmar dos veces: si el modelo escribe su propio resumen al llegar a la
    etapa de confirmación, el motor manda en su lugar el resumen único con la confirmación
    (`_pedir_accion_debida`). La tarjeta y el texto de `confirmacion_requerida` ya no le piden al modelo
    que arme el resumen.
  - Un correo con el dominio mal escrito (`gmai.com`, `hotmial.com`, `outlok.com`…) ya no se guarda sin
    preguntar «¿quisiste decir @gmail.com?»; si el cliente insiste, se acepta.
  - El lead del cliente se completa con el nombre, el correo y el teléfono que da en un recorrido
    (evento `procedure.slots_set`) y al crear el pedido.
  - `consultar_estado_pedido` y `verificar_pago_pedido` dicen el estado de un pedido de plan
    (`estado_plan`, `acceso`) en vez de «en preparación». Cambia también con el motor apagado (es un
    bug de dominio).
  - `cob_suscripcion_check` (pack-cobranza) ya no toma «No encontré una suscripción activa» como una
    suscripción creada; la polaridad (preguntas, negaciones y condicionales) pasa a
    `core/shared/polaridad.py`. Ese falso positivo era el que dejaba la respuesta neutral «Dejame
    verificar eso y te confirmo».
  - Sin suscripción ligada a la conversación, `sub_consultar` ofrece pasar con una persona que verifique
    la identidad o contratar un plan.
  - La auditoría de los comandos del hilo guarda los códigos de error.
