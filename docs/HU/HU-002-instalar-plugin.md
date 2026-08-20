# HU-002 · Instalar un plugin por CLI

**Como** developer, **quiero** `agent-core plugin install <nombre>`, **para** agregar
una capacidad completa (tools, tablas, cron, UI) sin escribir integración.

## Criterios de aceptación

- [ ] Resuelve primero `./plugins/<nombre>/` local; si no existe, clona de
      `github.com/agent-core-plugins/<nombre>` (org configurable).
- [ ] `pip install -e` deja el código EMBEBIDO en el proceso (MONOLITH-MODEL.md).
- [ ] Corre las migraciones PROPIAS del plugin (version_table separada).
- [ ] Wizard de datos: esquema base vacío O importar desde PostgreSQL existente (HU-009).
- [ ] Wizard de exposición por entidad: ¿panel admin? ¿tools del agente?
- [ ] Actualiza el SDD (`entities.md`, `skills.md`, `changelog.md`) automáticamente.
- [ ] Si docker compose está corriendo: reinicia core-api/worker/beat automáticamente.
- [ ] `agent-core plugin remove` desinstala el paquete pero CONSERVA las tablas.

## Flujo técnico

`cli/commands/plugin.py` → `cli/registry.py` → `plugin_manager.scan()` →
`_run_plugin_migrations()` → wizards → `sdd_generator.render_all()` → docker restart.
