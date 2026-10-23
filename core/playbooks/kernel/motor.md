---
name: motor
description: Errores de negocio del motor de procedimientos (HU-AOS-05). El modelo los lee como resultado de una acción o de un comando y corrige; el kernel solo rellena nombres validados.
---
## fuera_de_etapa
La acción {accion} no corresponde a la etapa actual ({etapa}) del recorrido en curso. Usa solo las acciones de esta etapa; si el cliente quiere otra cosa, díselo y sigue con lo que falta.
## confirmacion_requerida
Antes de ejecutar {accion} necesito la confirmación explícita del cliente, y el sistema ya se la pide con su propio resumen: no escribas tú otro resumen ni le preguntes si confirma. Cuando el cliente confirme con un sí claro, llama confirmar_tarea y después {accion} con los mismos datos.
## falta_confirmacion
Falta la confirmación explícita del cliente: su último mensaje no es un sí claro, así que NO hay nada confirmado ni registrado. No digas que quedó confirmado, registrado o aceptado, ni prometas pasos siguientes (datos de pago, link, envío). Pídele con claridad que responda «Sí, confirmo» o que te diga qué quiere cambiar; una respuesta ambigua no cuenta.
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
## argumento_ligado
El valor de {dato} no coincide con el dato vigente del recorrido. Usa el de la tarjeta de estado u omítelo: el sistema lo completa.
## dato_bloqueado
El dato {dato} ya no se puede cambiar: el pedido de este recorrido ya se creó. Si el cliente quiere otra cosa, cancela este recorrido y empieza uno nuevo.
## sin_recorrido
La acción {accion} solo se puede usar dentro de un recorrido y no hay uno en curso. Inicia primero el recorrido con iniciar_procedimiento (procedure_id, uno de: {procedimientos}) y sigue sus etapas: la acción se ofrece cuando toca.
## fuera_de_etapa_falta
La acción {accion} no corresponde a la etapa actual ({etapa}) del recorrido en curso. Para pasar de esta etapa falta: {faltas}. Usa solo las acciones de esta etapa; si el cliente pregunta, explícale en palabras simples qué falta (nunca digas que te falta una herramienta).
## dato_no_dicho
El valor de {dato} no aparece en lo que escribió el cliente en esta conversación. Pídeselo al cliente o usa sus mismas palabras; no lo tomes del perfil del canal ni lo completes.
## confirmacion_vencida
La confirmación que se le pidió al cliente ya venció (pasó más de un turno). Vuelve a llamar {accion} con los mismos datos: el sistema le pedirá la confirmación de nuevo.
## confirmacion_repedida
La confirmación que se le pidió al cliente ya venció (pasó más de un turno): el sistema se la vuelve a pedir con su propio mensaje. No la pidas tú ni la des por hecha.
## fuera_del_agente
La acción {accion} no está disponible para este agente en esta conexión. No insistas con ella: resuelve con las acciones ofrecidas o, si el cliente necesita otra cosa, díselo y sigue con lo que falta.
## correo_dudoso
El correo de {dato} parece tener el dominio mal escrito. Antes de seguir, pregúntale al cliente si quiso decir @{sugerido}. Si dice que sí, guarda el correo con @{sugerido}; si confirma que está bien como lo escribió, vuelve a guardarlo tal cual.
