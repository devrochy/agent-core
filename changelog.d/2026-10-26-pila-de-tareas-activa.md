## Dos pedidos en un mensaje: la pila de tareas activa (HU-AOS-20, parte 2)

Con la bandera `pila` en `active` (`PUT /api/v1/admin/platform/tenants/{id}/colaboracion`), que además exige `aos_procedures` y el ruteo activo, el Coordinador abre la pila. El diseño está en la adenda HU-AOS-20 de ADR-0116.

- **Dos pedidos sin tarea en curso:** atiende el dueño del primero, que se abre en foco. Los demás se abren en pausa con su propio dueño, el especialista que declara el procedimiento. Si la estabilidad del dueño frena el cambio, no hay pila en ese turno.
- **Digresión:** con una tarea en foco, el pedido nuevo se encola en pausa.
- **Tope:** lo que no cabe en `procedures.maxStack` no se abre y queda anotado en la tarjeta. `maxStack = 1` apaga la pila.
- **Atomicidad:** todo se abre en una sola transacción con la conversación bloqueada.
- **Retomar:** sin tarea en foco y con tareas en pausa, `asignar` retoma la primera (después de completar, cancelar, escalar, cambiar de versión o un cierre fuera del turno). El router la lleva a su dueño.
- **Tarea retomada de otro agente en el mismo turno:** si al cancelar se retoma una pausada que es de otro agente, ese turno no ofrece sus acciones y la tarjeta avisa que la sigue ese agente.
- **Tarjeta:**
  - con tareas en pausa, guía de una sola pregunta (solo la de la tarea en foco);
  - de cada tarea en pausa, lo que falta, sin pedirlo;
  - los sobrantes;
  - el tope de pausadas pasa a ser `maxStack - 1`.
- **Handoff:** las tareas en pausa de otro especialista conservan su dueño. Ya no bloquean `pasar_a_agente` ni se reasignan en la fase 2.
