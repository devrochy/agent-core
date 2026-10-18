## Enrutamiento entre especialistas y modo sombra (2026-10-18)

- HU-AOS-17 (#378), ADR-0138. El Coordinador elige qué especialista atiende el turno, sin LLM: tarea en foco
  (sin clasificar) → reglas del KernelSpec (`routing.rules`: botón, palabras, hecho del cliente, etapa, canal,
  horario) → disparadores léxicos sobre los ejemplos de los procedimientos y del agente → el de siempre. Con
  estabilidad del dueño (`maxOwnerChangesPer6Turns`).
- Bandera nueva por negocio `tenants.aos_routing` (`off` por defecto, `shadow`, `active`; exige
  `aos_runtime = v3`), migración 0094. En sombra decide y registra en `core_decisions.shadow_route` pero atiende el
  de siempre; en activo escribe `route_source` (`task`, `rule`, `trigger`), `rule_id`, `candidates`, `confidence`
  y el dueño de la conversación. Una regla `start` hace que el motor inicie el procedimiento al empezar el turno.
- CLI del master: `agent-core db ruteo --tenant X --modo off|shadow|active` y
  `agent-core db reporte-ruteo --tenant X --dias 7` (precisión y recall por procedimiento de la sombra).
- El build de la release rechaza reglas que apuntan a un agente o procedimiento que no está (`ruteo_invalido`).
- Las tareas que inicia el motor llevan como dueño al especialista del turno.
- Golden de ruteo de 80 casos (`tests/fixtures/ruteo/golden.jsonl`). Con la bandera apagada no cambia nada.
