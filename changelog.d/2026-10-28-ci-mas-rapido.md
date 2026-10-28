## CI del backend con menos espera (2026-10-28)

- Job `nivel` aparte: `rapido`, `integracion` y `evals-replay` arrancan a la vez en vez de en cadena.
- Gitleaks con binario cacheado en el host (~30 s menos por corrida).
- Unitarios con `pytest -n 4` (pytest-xdist). Los ids de `parametrize` de `test_opciones_payload`, `test_purga_redis_claves` y `test_referencias_vigentes_cotizacion` pasan de `uuid4` a `uuid5` fijo para que la colección sea idéntica en cada worker.
- La integración sube `--durations=40` y el junit como artifact para decidir cómo repartirla. Adenda de ADR-0120.
