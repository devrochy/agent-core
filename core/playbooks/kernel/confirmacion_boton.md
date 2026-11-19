---
name: confirmacion_boton
description: Rótulos del mensaje de confirmación que arma el kernel (HU-AOS-09, core/kernel/confirmacion.py). El cuerpo se arma SOLO con los argumentos que el servidor va a ejecutar (los que firma el botón) y con los datos de la tarea, nunca con el texto del modelo. `encabezado` y `encabezado_dato` reciben {titulo} (título del procedimiento; pueden no usarlo); `linea` recibe {rotulo} y {valor}; `monto` recibe {valor}. `encabezado_pregunta` y `vineta` arman la pregunta del kernel por los datos que faltan cuando el turno agotó sus vueltas (adenda 2026-10-13). `instruccion_dato` cierra la confirmación de un DATO (sale siempre como texto y basta un sí). `sin_resumen` es el texto fijo, sin datos ni botón, cuando el kernel no puede armar un resumen fiel. El rótulo de cada dato es el que publica la habilidad (`register_rotulos`) o su nombre con espacios. Formato de la adenda 2026-10-13 de ADR-0113: encabezado breve, una viñeta por dato, montos al final, los documentos que se aceptan en su bloque (`encabezado_documentos`) y la instrucción en la última línea. Se ocultan los identificadores (nombres `id`/`*_id` y valores con forma de uuid), los valores no escalares, las credenciales y los datos de pago.
---
## encabezado
Antes de seguir, revisa que todo esté bien:
## encabezado_dato
¿Está bien este dato?
## linea
• {rotulo}: {valor}
## monto
${valor}
## verdadero
Sí
## falso
No
## boton
Confirmar
## encabezado_documentos
Documentos que aceptas al confirmar:
## instruccion_boton
Si todo está bien, toca «Confirmar»; si algo no, escríbeme qué cambiar.
## instruccion_texto
Si todo está bien, responde *Sí, confirmo*; si algo no, escríbeme qué cambiar.
## instruccion_boton_documentos
Si todo está bien y aceptas los términos, toca «Confirmar»; si algo no, escríbeme qué cambiar.
## instruccion_texto_documentos
Si todo está bien y aceptas los términos, responde *Sí, confirmo*; si algo no, escríbeme qué cambiar.
## sin_resumen
Antes de seguir necesito revisar los datos contigo. ¿Me confirmas de nuevo qué quieres hacer?
## instruccion_dato
Si está bien, responde «sí»; si no, escríbeme el dato correcto.
## encabezado_pregunta
Para seguir necesito estos datos:
## vineta
{numero}. {rotulo}
## cierre_pregunta
Puedes enviármelos en un solo mensaje.
