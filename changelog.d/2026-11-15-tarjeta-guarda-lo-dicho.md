## Tarjeta de estado: guarda lo que el cliente ya dijo y no ofrece correos (2026-11-15)

- Hallazgo de la prueba del plan Free por chat web en test: el `goal` de las etapas de un procedimiento no llega al
  prompt (el kernel no lo lee), así que los cambios de `contratar_plan` 2.0.1 en `elegir_plan` y `pagar` no cambiaron
  la conducta. El arreglo va en la tarjeta (`core/playbooks/kernel/estado-conversacion.md`), que sí ve el modelo.
- `pide_ahora`: si el cliente ya dijo un dato, aunque sea con sus palabras, se guarda con `actualizar_tarea` en la
  misma vuelta y no se le pide; los datos se piden en palabras simples, nunca por su nombre interno ni un código.
  El agente respondía «1. Plan (el código del plan que quieres contratar)» a «quiero contratar el plan Free».
- `dato_a_pedir`: el rótulo humano va primero (`1. "Plan" (se guarda como plan_code)`).
- `acciones`: no ofrecer mandar por correo ni por otro medio una confirmación o un resumen del recorrido (el agente
  ofrecía «¿Quieres que te envíe la confirmación al correo…?» tras inscribir).
- Sin cambios de pack ni de procedimiento: no hay que reconstruir releases; entra con el despliegue. Ningún cassette
  de replay contiene esas líneas (solo salen con una tarea en foco).
