## `reporte-ruteo` mide sin depender de que el agente inicie el procedimiento (2026-11-03)

- HU-AOS-17, ADR-0138 (adenda). El reporte comparaba la predicción del router con la primera tarea iniciada en la
  conversación. En `shadow` la inicia el agente por defecto; si no lo hace, «Reales» salía 0 y la precisión 0,00
  aunque la predicción fuera correcta (visto en `solvershub`: 7 aciertos, 0 falsas). Con `aos_routing = active` no se
  escribe `shadow_route` y el reporte quedaba vacío.
- `agent-core db reporte-ruteo --tenant X [--fuente golden|trafico|ambas] [--golden archivo]` ahora trae dos fuentes,
  cada una rotulada con lo que mide:
  1. **Golden de ruteo** (verdad por escenario) contra la release activa de cada conexión: no depende del tráfico ni
     del agente, y sirve en `off`, `shadow` y `active`. Lista los casos que fallan y los no aplicables. Un caso «ninguno» donde el router elige un procedimiento ajeno al catálogo del golden queda «sin verdad», no como falso positivo.
  2. **Tráfico real** con su origen (`sombra` o `activo`): en activo mide los disparadores (primer candidato) frente a
     lo que inicia el especialista; las reglas no se miden en activo.
- Una predicción en un turno donde no se inició nada es ahora «sin verdad», no un fallo; la precisión es «no medible»
  si no hay ninguna con verdad. Una leyenda explica por qué una fuente sale vacía (ruteo apagado, `active`,
  `aos_procedures` apagada, nadie inició nada) y se imprimen las banderas del negocio.
- El golden de ruteo pasa de `tests/fixtures/ruteo/golden.jsonl` a `evals/ruteo/golden.jsonl` (viaja en la imagen).
  Sin cambios de esquema ni de lo que se escribe en `core_decisions`.
- Runbook de migración § 3.4 con cómo leer cada fuente.
