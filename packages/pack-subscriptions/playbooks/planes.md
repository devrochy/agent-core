# Planes (pack_subscriptions)

Los planes y la inscripción viven en este pack. El precio, la periodicidad y el
cliente los pone el servidor: tú solo mandas el código del plan, los datos que
pide el procedimiento y si el cliente aceptó los términos.

## Informar planes
- Si el cliente solo pregunta (qué planes hay, cuánto cuestan, qué incluyen, si hay
  prueba), llama `sub_listar_planes` directamente (con `plan_code` para uno solo) y
  responde con lo que devolvió. No inicies `contratar_plan` solo para informar: el
  procedimiento se inicia cuando el cliente dice que quiere un plan.
- Nunca des un precio, un límite o una prueba que esa tool no devolvió. Si no hay
  planes publicados (`encontrado: false`), dilo y no inventes ninguno.
- Si el cliente trae un código de promoción, mándalo en `codigo_promocion` al
  cotizar; el descuento y el total los calcula el servidor.

## Cómo se contrata (una sola vía: procedimiento `contratar_plan`)
1. El cliente elige el plan → `sub_cotizar_inscripcion` devuelve el total real.
2. Se le muestra el resumen y se espera su confirmación → `sub_confirmar_inscripcion`.
3. Se inscribe con `sub_inscribir` (lleva la aceptación de los términos).
4. Si el plan cobra, se comparte el link con `sub_link_pago`; la suscripción se activa
   cuando el pago se confirma.

## Reglas
- Nunca afirmar que una suscripción «se activó» sin que la tool lo haya confirmado.
- Un plan gratuito no pide pago: la inscripción queda activa al inscribir.
- Si el cliente ya tiene una suscripción y pide otro plan, es un cambio de plan
  (`sub_cotizar_cambio_plan`), no una inscripción nueva.
