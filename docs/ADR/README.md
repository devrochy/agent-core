# Architectural Decision Records

| ADR | Decisión | Estado |
|---|---|---|
| [ADR-001](ADR-001-monolito-modular.md) | Monolito Modular con Adaptadores Selectivos (no hexagonal) | Aceptada |
| [ADR-002](ADR-002-plugins-embebidos.md) | Plugins embebidos in-process vía entry_points | Aceptada |
| [ADR-003](ADR-003-deepseek-default.md) | DeepSeek como LLM por defecto + difficulty routing | Aceptada |
| [ADR-004](ADR-004-memoria-3-capas.md) | Memoria en 3 capas + proveedor de embeddings configurable | Aceptada |
| [ADR-005](ADR-005-migraciones-por-plugin.md) | Migraciones Alembic por plugin (version_table separada) | Aceptada |
| [ADR-006](ADR-006-colas-debounce-idempotencia.md) | Redis Stream + debounce ZSet + idempotencia 3 capas | Aceptada |
| [ADR-007](ADR-007-panel-ssr-formgenerator.md) | Panel SSR Jinja2 + FormGenerator desde Pydantic | Aceptada |
| [ADR-008](ADR-008-single-tenant-sdd-vivo.md) | Single-tenant por despliegue + SDD vivo generado por CLI | Aceptada |
