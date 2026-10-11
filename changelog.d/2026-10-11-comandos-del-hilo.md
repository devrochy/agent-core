## Comandos del hilo y verificador sobre el estado (2026-10-11)

- HU-AOS-06: con `tenants.aos_procedures`, el modelo ve los comandos del hilo como tools del core
  (`iniciar_procedimiento`, `actualizar_tarea`, `confirmar_tarea`, `cancelar_tarea`), solo los que
  aplican al estado y con JSON Schema estricto. El motor los aplica y el resultado vuelve con la
  tarjeta de estado actualizada y las acciones de la etapa vigente. Un id de procedimiento fuera de
  los candidatos se rechaza sin crear la tarea.
- El verificador (ADR-0119) usa el estado de las tareas como evidencia: los datos que fijó una
  acción respaldan montos, referencias y los cierres de la tarea en foco. Una referencia afirmada de
  la familia que fijó el servidor (`OC-…`) que no coincide falla con `referencia_sin_evidencia`.
- Con la bandera apagada no cambia nada: ni tools, ni prompt, ni veredictos.
- Adendas en ADR-0113 y ADR-0119 (esta última requiere la aprobación del dueño).
