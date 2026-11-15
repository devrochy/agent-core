## Contratar un plan: hallazgos de la prueba del Free en test (2026-11-15)

- `pack_subscriptions` 0.6.1, `pack_subscriptions.contratar_plan` 2.0.1. Prueba por chat web en test (solvershub, plan Free).
- La etapa `elegir_plan` pide el plan por su nombre y fija `plan_code` con las palabras del cliente: el agente pedía «el código del plan».
- El resumen que el cliente confirma ya no repite el plan («Plan: Free mensual» y «Plan: PLAN-FREE»): la línea del pack se llama `plan_code`, como el dato de la tarea, y el kernel no la vuelve a sumar.
- `sub_listar_planes` con un `plan_code` que no existe (p. ej. «free» por «PLAN-FREE») devuelve el catálogo con `codigo_no_encontrado` en vez de un error: el modelo elige el código real sin otra vuelta.
- La etapa `pagar` dice el número y el estado de la suscripción y no ofrece mandar una confirmación por correo (el acceso lo manda el sistema).
- Tras desplegar, reconstruir las releases de los negocios que declaran `contratar_plan` para que tomen 2.0.1.
