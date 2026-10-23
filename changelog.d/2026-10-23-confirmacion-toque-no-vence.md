## Un toque del botón que no confirma ya no vence la petición (2026-10-23)

- Ciclo de confirmación visto en test: el resumen único sale con el botón «Confirmar»; si el toque no
  se acepta, el título del botón llegaba como un mensaje del cliente posterior al resumen y vencía la
  petición, así que el «Sí, confirmo» que pedía el propio aviso salía vencido, el kernel reenviaba el
  resumen (con el mismo botón) y el cliente volvía a tocarlo, sin crear el pedido nunca.
- El kernel ahora vuelve a registrar la MISMA petición (mismo hash y huella del resumen, marca
  `renovada`) cuando rechaza un toque de la petición vigente (`ejecucion._renovar_peticion`), en el
  consumer y en el turno del toque. Una petición ya vencida no se revive, y un «sí» a secas sigue
  venciéndola (la regla de vigencia del turno anterior no cambia).
- El rechazo de un toque deja de ser invisible: `motor_boton_confirmar_rechazado` (warning) con el
  motivo (misma tarea, seq del botón y de la petición). Falta aislar por qué el toque no se aceptó
  en test; ese log lo muestra en la próxima prueba.
