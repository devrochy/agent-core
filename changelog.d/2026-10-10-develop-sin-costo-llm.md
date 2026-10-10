## Develop no gasta LLM (2026-10-10)

- CI: el gate de evals y journeys del push a `develop` corre en replay (sin `LLM_API_KEY`); el LLM real queda
  solo en la PR de release hacia `main`.
- `evals-nightly.yml` deja de correr cada noche: se lanza a mano con `k` = 2 por defecto.
- Adenda de ADR-0120 y ajustes en ADR-0119 y `evals/README.md`.
