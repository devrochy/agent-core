## El verificador revisa horarios y preguntas (HU-AOS-20, parte 3)

Cierra HU-AOS-20 (adenda HU-AOS-20 §7 de ADR-0116). Solo con el motor de procedimientos activo.

- **Horarios sin respaldo (`horario_sin_evidencia`):** toda hora o fecha que el asistente afirma tiene que salir de lo que hizo el sistema.
  - **Respaldan:**
    - los `free_slots` de la agenda;
    - cualquier instante o fecha ISO que devolvió el servidor (la cita creada, el próximo cargo);
    - los datos de las tareas que fijó una acción;
    - el horario del negocio que trae el prompt;
    - hoy.
  - **No respaldan nunca:**
    - las horas ocupadas (`busy_slots`);
    - el texto de un error;
    - lo que dijo el cliente, salvo como eco de un dato que la tarea tiene fijado del cliente.
  - **No cuentan como afirmación:** las preguntas, las negaciones ni los condicionales.
  - **Formatos que reconoce:** «3 p. m.», «a las 3», «15:00», «3 de la tarde», «mediodía», «el jueves 29», «30 de octubre», «mañana».
- **Más preguntas que `composition.maxQuestions` (`demasiadas_preguntas`).** Pedir datos en serie («¿Tu nombre? ¿Y tu correo?» o una lista numerada) cuenta como una sola pregunta.
- **Modo por negocio:** `PUT /api/v1/admin/platform/tenants/{id}/colaboracion` con `verificar_horarios` o `verificar_preguntas`.
  - `shadow` (por defecto) solo cuenta, como `unverifiable` en `turn_outcomes.verifier`. La sombra ahora se registra aunque el turno no tenga otras extracciones.
  - `active` rechaza la respuesta y aplica el reintento del negocio, con el texto de las secciones nuevas del playbook `verificador-estado`.
- **Eval del verificador:**
  - `medir` admite los modos, el reloj y el contexto del sistema y del cliente de cada caso;
  - se suman 18 casos `sintetico-horario` y `sintetico-preguntas`;
  - con las dos validaciones en `active`, las respuestas reales siguen bajo el umbral (1 falso positivo de 217).
