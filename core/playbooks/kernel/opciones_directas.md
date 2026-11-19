---
name: opciones_directas
description: Textos fijos del resolver del toque de las opciones directas (SPEC-40, ADR-0172, core/kernel/opciones/textos.py). No se cargan en el system prompt del LLM: el toque no pasa por el modelo. Son las respuestas del sistema cuando el botón venció, su versión se retiró, ya se atendió, está en curso, no se pudo confirmar el resultado, la confirmación venció o se canceló, o algo falló sin una respuesta propia de la opción; `sensible_por_texto` sale cuando el cliente contesta con un número o un título a un menú de texto y la opción pide confirmación (v1: esas no se ejecutan por texto); `ok_generico` sale cuando la respuesta de éxito de la opción cita un dato del resultado que no llegó. La respuesta de cada opción (`respuesta_ok` y `respuesta_error`) la escribe el dueño en la definición. `boton_confirmar` y `boton_cancelar` son los títulos de los botones de la confirmación (20 caracteres como máximo); `instruccion_boton` y `instruccion_texto` cierran el resumen de la acción que el cliente confirma.
---
## envio_vencido
Este mensaje ya venció, así que no puedo ejecutar esa opción. Escríbeme si necesitas algo y con gusto te ayudo.
## opcion_retirada
Esa opción ya no está disponible.
## ya_atendido
Ya registramos tu elección en este mensaje.
## en_curso
Estoy procesando tu solicitud. En un momento te confirmo.
## sin_confirmar
No pude confirmar el resultado de tu solicitud anterior, así que no la repetí. Escríbeme si necesitas ayuda.
## confirmacion_pendiente
Tienes una confirmación pendiente: toca «Confirmar» o «Cancelar» en el mensaje anterior.
## confirmacion_vencida
Esa confirmación ya venció. Toca de nuevo la opción si quieres continuar.
## cancelado
Listo, cancelé la solicitud. No se hizo ningún cambio.
## error_generico
No pude completar tu solicitud en este momento. Escríbeme si necesitas ayuda.
## ok_generico
Listo, tu solicitud quedó registrada.
## confirmacion_no_disponible
No pude pedirte la confirmación por este canal, así que no hice ningún cambio.
## sensible_por_texto
Esa opción no se puede hacer respondiendo con un número, así que no hice ningún cambio. Cuéntame qué necesitas y te atiendo por aquí.
## boton_confirmar
Confirmar
## boton_cancelar
Cancelar
## instruccion_boton
Si todo está bien, toca «Confirmar»; si no, toca «Cancelar».
## instruccion_texto
Si todo está bien, usa el botón «Confirmar»; si no, usa «Cancelar».
