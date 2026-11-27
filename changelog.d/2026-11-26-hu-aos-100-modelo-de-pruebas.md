## Modelo de datos de las pruebas del agente (HU-AOS-100) (2026-11-26)

- Migración `0160_pruebas_del_agente`: tablas `agent_test_cases`, `agent_test_runs` y `agent_test_results` con RLS
  forzada y borrado lógico (ADR-0212 §1). Un índice parcial deja una sola corrida activa por negocio y otro hace
  único el título de un escenario por agente. `usage_ledger.kind` admite `prueba_agente`.
- `core/billing/tipos_de_uso.py`: `prueba_agente` no es turno ni actividad del agente.
- `core/billing/creditos.py::medir_creditos`: las filas `prueba_agente` suman al cubo del playground con una sola
  fórmula, `max(0, playground vivo + pruebas − 300)`; la conversación de una corrida en curso
  (`state.prueba_corrida`) no suma hasta que se consolida, y `_sin_playground` también la excluye. Sin rutas
  nuevas: las de escenarios y corridas llegan con HU-AOS-101 y HU-AOS-102.
