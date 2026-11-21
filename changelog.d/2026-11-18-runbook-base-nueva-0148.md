## Runbook de base nueva: esperado con la 0148 (2026-11-18)

- `docs/runbooks/arranque-base-nueva.md`: en 7.2 el esperado pasa a `0` y en 7.3 a todo `[OK]` con código 0, ahora que la `0148` (agent-core#845) fuerza RLS en `meta_private.meta_tokens`. El registro del ensayo conserva lo que salió antes de esa migración.
