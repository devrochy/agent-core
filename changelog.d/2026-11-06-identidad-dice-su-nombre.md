## El asistente dice su nombre cuando se lo preguntan (2026-11-06)

- El playbook `identidad` ahora afirma «Tu nombre es {assistant_name}» y le pide responder con ese nombre si el cliente pregunta cómo se llama o quién es. Antes solo decía «Eres {assistant_name}, el asistente virtual de: …» y el modelo a veces respondía «el asistente virtual de la tienda» sin el nombre (agent-core#674).
- Cambia el prompt de todos los negocios sin override de `identidad`: las grabaciones de evals (`evals/cassettes/`) hay que regrabarlas con OK del dueño.
