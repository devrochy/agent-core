## Motor de procedimientos: confirmar nombrando lo que dice el resumen, y el modelo no puede decir «quedó registrada tu confirmación» sin pedido (HU-AOS-15)

La segunda medición pass^4 del 2026-10-24 (corrida #10, tras el arreglo de «Todo bien.») bajó otra vez a 75 % con motor: el cliente
escribió «Sí, confirmo. Todo bien con el anual.», el clasificador no lo tomó por confirmación («anual» no está en el léxico), el
modelo dijo «Ya quedó registrada tu confirmación del Plan Premium anual…» y `crear_pedido` nunca se llamó. Dos arreglos:

- **Eco del resumen** (adenda 2026-10-24 de ADR-0113): `clasificador.es_confirmacion(..., eco=)`. Las palabras del mensaje del kernel
  que el cliente contesta (el ÚNICO mensaje de salida posterior a la petición) acompañan a la marca de confirmación como cortesía,
  nunca como núcleo. «Sí, confirmo. Todo bien con el anual» confirma ante un resumen de «Premium anual»; «…con el mensual» ante ese
  mismo resumen NO (el cliente nombra lo que no vio), ni «todo bien con el anual» sin marca, ni una reserva, negación o pregunta.
  Las palabras de negación del léxico nunca se vuelven eco. Solo para acciones y etapas; el dato personal sigue con el asentimiento.
  Léxico: `con`, `el`, `la` pasan a `cortesia`.
- **Afirmaciones de `contratar_plan` 1.3.0** (pack-commerce 2.4.0): «registrada tu confirmación», «tu confirmación quedó
  registrada», «confirmación registrada», «registrado tu pedido» y «pedido registrado» exigen `crear_pedido` (`slot:order_id`);
  sin él el verificador devuelve `status_sin_evidencia` y el modelo corrige.
- **Al desplegar:** los agentes publicados fijan `contratar_plan` 1.2.0 en su release (siguen con las frases de antes hasta
  republicar); las tareas abiertas de 1.2.0 sin release dejan de estar gobernadas (flujo v2 de respaldo), como en la 1.2.0.
