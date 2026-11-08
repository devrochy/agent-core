## Observabilidad B4: `GET /healthz/deep` (ADR-0188) (2026-11-08)

- Ruta nueva y pública (prefijo `/healthz`): `SELECT 1` y `PING` a Redis con tope de 1 s, y la edad del latido `worker:heartbeat` (fallo si pasa de 180 s o no existe). Responde 200 o 503 con `{status, componentes: {db, redis, worker}}` y solo `ok`/`fallo`, sin DSN ni mensajes. El resultado se guarda 5 s (también el 503) y las peticiones simultáneas comparten una medición.
- `/healthz` no cambia: lo siguen usando los health checks de Coolify y de compose.
