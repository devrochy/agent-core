## Conversación ordenada con el motor de procedimientos (2026-10-13)

Solo con `tenants.aos_procedures`; con la bandera apagada (y en sombra) nada cambia. Adenda
2026-10-13 de ADR-0113 y de ADR-0125. Refs #368.

- **Sin escalamiento falso por el tope de vueltas:** las vueltas de solo comandos del hilo no gastan
  el tope (hasta 3) y, si se agota con una tarea en foco, el kernel pide lo que falta en vez de
  decir "te comunico con una persona".
- **Datos en orden y en un solo mensaje:** la tarjeta pide todos los datos que faltan como lista
  numerada (negocio, tipo, nombre, documento, correo, teléfono, forma de pago) y, si el cliente da una
  parte, solo lo que falta. Al cambiar de etapa a mitad del turno el modelo ve la tarjeta vigente.
  `contratar_plan` 1.2.0 (pack-commerce 2.3.0).
- **Un resumen único** con el plan, el total, los datos del cliente (el correo ya no se confirma
  aparte) y, en el vendedor de planes, los términos, la privacidad y el DPA, que se aceptan con el
  mismo «Sí, confirmo». `crear_pedido` sigue exigiendo la aceptación registrada.
- **Mensajes del kernel claros:** viñetas, monto como $600.000 y la instrucción en una línea al
  final; rótulos humanos de los datos que publica cada pack (`register_rotulos`).
- **Tipo de negocio por sinónimos** ("tienda" → comercio, "pizzería" → restaurante), metadato de los
  presets.
- **Un solo mensaje con el pedido y el link:** no se repite el link que el texto ya trae ni el
  "✅ quedó confirmado" con pago en línea.
