## `must_not_call_tools` respeta la lista de tools del escenario (2026-10-09)

- `evals/runner.py`: la aserción `must_not_call_tools` con una lista de nombres solo falla si el asistente
  llama alguna de esas tools; antes cualquier valor truthy se evaluaba como «no debe llamar ninguna tool»
  (el escenario 17 `limite_de_altas` del tenant solvershub fallaba por un `set_conversation_note` que no
  estaba en la lista). `true` sigue prohibiendo cualquier tool; `false`, la lista vacía o la clave ausente
  no restringen nada, y otro tipo (un string suelto, un número) hace fallar el escenario como inválido.
  Tests en `tests/test_evals_runner.py`.
