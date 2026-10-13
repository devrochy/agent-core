---
name: confirmacion_boton
description: Rótulos del mensaje de confirmación que arma el kernel (HU-AOS-09, core/kernel/confirmacion.py). El cuerpo se arma SOLO con los argumentos que el servidor va a ejecutar (los que firma el botón) y con los datos de la tarea, nunca con el texto del modelo. `encabezado` recibe {titulo} (título del procedimiento); `linea` recibe {rotulo} y {valor}; `monto` recibe {valor}. `instruccion_dato` cierra la confirmación de un DATO (sale siempre como texto y basta un sí). `sin_resumen` es el texto fijo, sin datos ni botón, cuando el kernel no puede armar un resumen fiel. El rótulo de cada dato es su nombre con espacios (el core no conoce el vocabulario de los packs; el resumen propio de una acción de un pack llega con HU-AOS-07, adenda de ADR-0113). Se ocultan los identificadores (nombres `id`/`*_id` y valores con forma de uuid), los valores no escalares, las credenciales y los datos de pago.
---
## encabezado
Para confirmar: {titulo}
## linea
{rotulo}: {valor}
## monto
${valor}
## verdadero
Sí
## falso
No
## boton
Confirmar
## instruccion_boton
Si todo está bien, toca «Confirmar». Si algo no, escríbeme qué cambiar.
## instruccion_texto
Si todo está bien, responde «confirmo». Si algo no, escríbeme qué cambiar.
## sin_resumen
Antes de seguir necesito revisar los datos contigo. ¿Me confirmas de nuevo qué quieres hacer?
## instruccion_dato
Si está bien, responde «sí». Si no, escríbeme el dato correcto.

