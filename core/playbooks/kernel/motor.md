---
name: motor
description: Errores de negocio del motor de procedimientos (HU-AOS-05). El modelo los lee como resultado de una acción o de un comando y corrige; el kernel solo rellena nombres validados.
---
## fuera_de_etapa
La acción {accion} no corresponde a la etapa actual ({etapa}) del recorrido en curso. Usa solo las acciones de esta etapa; si el cliente quiere otra cosa, díselo y sigue con lo que falta.
## confirmacion_requerida
Antes de ejecutar {accion} necesito la confirmación explícita del cliente. Pídesela mostrando lo que se va a hacer; cuando confirme, vuelve a llamar la acción con los mismos datos.
## falta_confirmacion
Falta la confirmación explícita del cliente. Vuelve a pedírsela con claridad; una respuesta ambigua no cuenta.
## sin_confirmacion_pendiente
No hay nada pendiente de confirmar en el recorrido en curso.
## sin_tarea
No hay un recorrido en curso.
## dato_desconocido
El dato {dato} no existe en este recorrido.
## dato_del_servidor
El dato {dato} lo fija el sistema, no se puede escribir.
## tipo_invalido
El valor de {dato} no es válido (se espera {esperado}). Pídeselo de nuevo al cliente; no lo adivines.
## enum_invalido
El valor de {dato} no es una de las opciones permitidas. Ofrece las opciones reales al cliente.
## formato_invalido
El valor de {dato} no tiene un formato válido ({esperado}). Pídeselo de nuevo al cliente.
## resolver_rechazo
{dato}: {error}
## resolver_no_disponible
No pude validar {dato} en este momento. Pide disculpas y vuelve a intentarlo más tarde.
## procedimiento_no_disponible
Ese recorrido no está disponible ahora.
## tarea_en_curso
Ya hay un recorrido en curso. Termínalo o cancélalo antes de empezar otro.
## error_interno
No pude registrar el paso del recorrido. Pide disculpas y vuelve a intentarlo.
