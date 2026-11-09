## Modelo C4 canónico en Structurizr (2026-11-09)

- ADR-0189. `docs/architecture/workspace.dsl` es la fuente única de los diagramas C4: contexto, contenedores, componentes de `core-api`, `celery-worker` y packs, y despliegue por entorno.
- `docs/architecture/c4.md` pasa a ser generado con `scripts/export_c4.py`.
