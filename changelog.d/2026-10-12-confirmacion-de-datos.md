## Agent OS: confirmaciones de datos visibles y sí a secas para datos (2026-10-12)

- Arreglo de la prueba real en test (`contratar_plan`): la confirmación del correo se pidió en
  `elegir_plan`, el cambio de etapa la venció, nadie la volvió a pedir y la tarea quedó atascada en
  `datos`. Solo con `tenants.aos_procedures`; sin migración. El gate en replay pasa sin regrabar.
- **La confirmación de un dato se pide en la etapa que la exige**, cuando es lo único que falta, y sale
  el mensaje del kernel, siempre como texto y sin montos. Al empezar el turno, una tarea atascada así se
  desatasca.
- **"Sí" a secas confirma un DATO personal** (`pii`) cuando contesta directo el mensaje del kernel que
  lo pidió. Lo confirma el kernel al empezar el turno, o `confirmar_tarea`. Acciones y etapas siguen con el sí explícito. Clasificador: asentimiento
  para datos (set nuevo `dato.jsonl`, precisión 1.0) y mejor recall del estricto sin bajar su precisión
  (100 % sobre 213 frases).
- **La tarjeta de estado y el error `fuera_de_etapa` dicen qué falta para salir de la etapa**, así el
  modelo no inventa que le falta una herramienta.
- **Un dato personal que el cliente no escribió en la conversación no se acepta.** Caso real: el nombre
  del perfil de WhatsApp. El teléfono del canal vale solo para un dato de teléfono.
- **Una confirmación de acción vencida se vuelve a pedir** cuando el modelo repite la acción. Ya no
  queda sin salida en canales sin botón.
- Adenda de ADR-0113, en estado propuesta.
