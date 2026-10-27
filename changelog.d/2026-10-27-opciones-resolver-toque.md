## Opciones directas: el resolver del toque (SPEC-40, HU-OPC-03, 2026-10-27)

- Un toque en una opción (`opt:`/`optc:`/`optn:`) ejecuta la acción de su definición **sin pasar por el LLM**
  y responde con el texto fijo de la opción. El gancho está en `consumer.py`, tras persistir el mensaje y
  las políticas de entrada y antes de `entrada_sin_llm`. Va detrás de la bandera `opciones_directas.habilitada`
  (apagada por defecto): apagada, el mensaje sigue el camino de siempre.
- `core/kernel/opciones/resolver.py`: firma y pertenencia a la conversación (un payload que no valida se
  descarta con warning y métrica, sin responder), vencimiento y versión retirada con texto fijo, modo
  exclusivo, una sola ejecución por reclamo atómico de la fila (`option_activations`, `UNIQUE(tenant_id,
  dedupe_key)`), reintento de webhook idempotente y `running` sin terminar que no se reejecuta.
- La tool corre por `_execute_tool_safely` (nuevo parámetro `ya_confirmada`) con `idempotency_key =
  <conversación>:<activation_id>`. Antes de ejecutar se revalida fail-closed: tool apta, pack activo,
  binding explícito y habilitado (sin el fail-open del loop), no gobernada por un procedimiento; los
  argumentos salen solo de `args_fijos` y del contexto del envío y se validan contra el schema de la tool
  (deriva → respuesta de error + log `option_args_drift`).
- Acciones con confirmación: botones `optc:`/`optn:` con `confirmacion.armar` (nuevo parámetro
  `instrucciones`); solo `optc:` ejecuta, con los argumentos que el cliente vio (hash firmado).
- Textos del sistema en el playbook del kernel `opciones_directas.md`. Auditoría `option_tap`
  (`actor="option:<key>"`, ids y claves sin valores), log por toque y métricas `option_taps_total{outcome}`,
  `option_tap_latency_seconds`, `option_tool_errors_total{tool}`.
- Sin migración ni rutas nuevas.
