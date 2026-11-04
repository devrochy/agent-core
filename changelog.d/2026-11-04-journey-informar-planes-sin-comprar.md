## Journey de informar planes sin comprar (2026-11-04)

- Journey `procedimientos/05-informar-planes-sin-comprar` (ADR-0185 / HU-SUB-37): el cliente solo pregunta por planes y el asistente usa `sub_listar_planes`, no inicia el procedimiento de compra y no deja tarea abierta. El runner de journeys expone `{conversacion.id}` para los pasos `verifica`. Sin grabación: el set `procedimientos` corre solo con LLM real a demanda.
