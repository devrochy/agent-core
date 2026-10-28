## CI del backend sin el job `nivel` (2026-10-28)

- `rapido`, `integracion` y `evals-replay` arrancan sin esperar a un job intermedio que hacía cola de runner por 10–30 s de trabajo. El nivel se lee del evento y las rutas relevantes las calcula el primer paso de `integracion`. Adenda de ADR-0120.
