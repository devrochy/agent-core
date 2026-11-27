## Corredor de las pruebas del agente (HU-AOS-103) (2026-11-26)

- `core/pruebas/corredor.py`: corre una corrida `en_cola` escenario por escenario y intento por intento (`k`) sobre el
  BORRADOR del agente, en el sandbox del playground, con una conversación de prueba propia por intento
  (`state.prueba_corrida`, cliente sintético `panel-prueba-<run_id>`). El cliente simulado conversa con el loop real
  hasta `[OBJETIVO_CUMPLIDO]`, `[ME_RINDO]`, un relevo a una persona o 12 turnos. Al terminar cada intento se suma el
  gasto de los turnos y del simulador en UNA fila `prueba_agente` sin conversación y se purga, en la misma
  transacción: no se pierde ni se cobra dos veces. Termina `terminada`, `cancelada`, `cortada_por_tope` (más de 1,5× el
  estimado, medido tras cada intento), `cortada_por_saldo` o `fallo`; siempre se cobra solo lo gastado.
- `core/pruebas/juez.py`: primero el chequeo determinista de tools (una prohibida llamada o una esperada ausente falla
  sin llamar al LLM); después el juez con `generar_estructurado` (`kind='prueba_agente'`), la transcripción en JSON como
  dato y un modelo fijo de plataforma (`PRUEBAS_MODELO`, vacío = el de plataforma). Una tool con efectos sin simulador
  deja el escenario en «no se pudo probar», sin juez. Un juez caído o con salida inválida tampoco cuenta como falla.
- Los prompts son playbooks del core: `cliente_simulado` (el simulador pasa de `evals/` a `core/pruebas/simulador.py`;
  `evals/simulador.py` lo reexporta; el objetivo y los datos del escenario entran como dato aparte, no en las
  instrucciones) y `juez_de_pruebas`.
- Tarea Celery `core.pruebas.correr` (`core/tasks/pruebas_tasks.py`), en el worker y la cola de siempre. El reclamo
  `en_cola → corriendo` hace de candado por negocio. Cron `core.pruebas_barrido` (cada 30 min): una corrida activa de
  más de 2 h pasa a `fallo` y sus conversaciones huérfanas se consolidan y purgan.
- API: `POST /admin/agents/{agent_id}/pruebas/corridas` ahora manda la corrida al worker (si el broker falla queda
  `fallo` y responde 503 `cola_no_disponible`); nuevo `POST …/corridas/{run_id}/cancelar`. Siguen detrás de
  `PRUEBAS_AGENTE_ENABLED` (apagada por defecto).
- Nota: la corrida usa el borrador del agente (todo especialista lo tiene) y la cola por defecto: no hay `-Q` ni
  `task_routes` en el repo y una cola con nombre sin worker dejaría las corridas en espera.
