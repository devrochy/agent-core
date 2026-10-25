## Handoff entre agentes en dos fases (HU-AOS-19, parte 1)

Primer uso real de la guarda A2A de HU-AOS-18: un agente puede pasarle la conversación a otro y el cliente no tiene que repetir nada. Diseño en la adenda HU-AOS-19 de ADR-0116, que trae también la parte 2 (encargar y consultar, con contratos 3.2.0, adenda de ADR-0113).

- **Comando `pasar_a_agente(agente)`.**
  - Se ofrece solo con el motor activo, una release con grafo congelado y especialistas alcanzables con `handoff` que declaren el procedimiento de cada tarea abierta. Sin grafo, el prompt queda igual.
  - `agente` es un enum de keys; los textos viven en el playbook `comandos`.
- **Fase 1 (en el turno):** la guarda decide (`owner_frozen` frena el ping-pong con el mismo límite del router) y el turno guarda el handoff pendiente.
- **Fase 2 (`core/kernel/handoff.py`, al cerrar el turno y antes de `turn_outcomes`).**
  - Si el turno terminó `replied` o `silent`, bajo lock y si el dueño sigue siendo el mismo:
    - cambia el dueño de la conversación;
    - escribe `owner_changed` en cada tarea abierta (la tarea viaja intacta);
    - registra `handoff_applied` en el ledger y pone `final_instance_id` = destino.
  - Si el turno falló, escaló o terminó `neutral`, o si otro turno ya cambió el dueño, se escribe `cancelled`.
  - Nunca lanza, así que un fallo no duplica la respuesta.
- **Otros cambios:**
  - `ArtefactosDeRelease.a2a_graph`;
  - `Limites.del_kernel`;
  - `a2a.liberar`, `a2a.cerrar_handoff` y `GrafoCongelado.sin`;
  - la conversación A2A del turno se cierra al terminar el turno.
