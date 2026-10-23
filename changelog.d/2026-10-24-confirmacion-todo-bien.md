## Motor de procedimientos: «Sí, confirmo. Todo bien.» confirma, y el modelo no puede darla por hecha (HU-AOS-15)

La medición pass^4 del 2026-10-23 (corrida #8) falló un intento de `cambia-de-plan-antes-de-confirmar`: el cliente escribió
«Sí, confirmo. Todo bien.», el motor no lo tomó por confirmación (con razón: «bien» no estaba en el léxico y el clasificador
exige que todas las palabras estén), el modelo recibió tres veces «falta la confirmación», aun así contestó «ya quedó
registrada tu confirmación… en un momento te comparto los datos de la cuenta» y el pedido nunca se creó.

- **Léxico de confirmación** (`core/playbooks/kernel/confirmacion.md`): «bien» pasa a `cortesia`. Solo acompaña a una marca de
  confirmación: «confirmo, todo bien» confirma; «todo bien», «sí, todo bien» o «bien, gracias» siguen sin confirmar (decisión del
  dueño: lo ambiguo no confirma). Set etiquetado con 14 filas nuevas, precisión 1.0.
- **Mensaje al modelo** (`core/playbooks/kernel/motor.md`, `falta_confirmacion`): ahora dice que NO hay nada confirmado ni
  registrado y le prohíbe decir que quedó confirmado o prometer pasos siguientes.
- No se tocó la regla del clasificador ni las frases de las afirmaciones del procedimiento (cambiarlas exige subir la versión que
  los agentes publicados fijan).
