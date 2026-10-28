## Integración en paralelo con pytest-xdist (2026-10-28)

- `pytest tests/integration -n 3 --dist loadfile` en el CI: cada worker clona la base migrada (`agent_core_gwN`) y usa su propia base de Redis y su `TENANTS_ROOT` (`tests/integration/_aislamiento_xdist.py`). La corrida serial no cambia. Medido: 23 min en serie → 5–10 min según la carga del host.
- Adenda de ADR-0120.
