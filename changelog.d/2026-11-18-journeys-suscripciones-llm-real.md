## Suscripciones con LLM real: cambio de plan, pagar el período y journeys al día (2026-11-18, HU-ARCH-28)

La primera noche de evals con LLM real (run 44357972704) dejó los journeys de suscripciones en rojo (pass^4 58 %).

- `pack_subscriptions` 0.8.0:
  - Resumen propio de `sub_aplicar_cambio_plan` con la cotización vigente: «Cambio de plan: del Plan Basic al Plan
    Premium», lo que paga hoy, el precio nuevo y cuándo aplica. Antes salían los argumentos crudos («Plan:
    PLAN-PREMIUM») y el monto con el rótulo de una inscripción («Total del primer cargo»).
  - Playbook `suscripciones`: seguía mandando a `sub_cambiar_plan`, que no se ofrece desde HU-AOS-69 (motor siempre
    encendido); el asistente decía que no podía cambiar el plan y escalaba. Ahora describe el procedimiento «Cambiar
    de plan» y que pagar el período de una suscripción es `sub_link_pago`, no «Contratar un plan».
  - `contratar_plan` 2.2.0: el disparador aclara que no es para pagar el período ni para cambiar de plan.
  - `sub_consultar` trae los cargos recientes de la suscripción (`list_for_customer` devolvía siempre `cargos: []`).
- Journeys: 08 y 09 esperan `sub_aplicar_cambio_plan` (no `sub_cambiar_plan`) e insisten con «Sí, confirmo el Plan
  …» («cambio» es negación en el léxico de confirmación); en 12 el paso de los datos ya no exige
  `sub_confirmar_inscripcion`, que solo corre después del «Sí, confirmo» del paso siguiente.
- `pack_commerce` 4.3.0, `pago_en_linea` 1.1.0: el disparador dice que no es para la mensualidad de una
  suscripción y la etapa `elegir_pedido` cierra el recorrido (`cancelar_tarea`) y usa `sub_link_pago` si el cliente
  habla del cobro de su suscripción. En la segunda noche, «pasame el link» abría este recorrido y le pedía un número
  de pedido a un suscriptor.
