## CI: modelo C4 sin drift (2026-11-09)

- El job `rapido` levanta `structurizr/mcp` en versión fija, valida `docs/architecture/workspace.dsl` y falla si `docs/architecture/c4.md` no coincide con lo que produce `scripts/export_c4.py`. Las PRs que no tocan el modelo se saltan el paso.
- `scripts/export_c4.py` ahora sí falla cuando el DSL es inválido: la herramienta `validate` devuelve el error como texto y el script lo ignoraba.
