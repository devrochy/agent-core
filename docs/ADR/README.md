# Architectural Decision Records (serie legacy 001-008)

> **SUPERSEDED.** Esta es la serie original del monolito (modelo single-tenant).
> La serie vigente vive en [`docs/adr/`](../adr/) (0001-0021, multi-tenant con RLS).
> Estos ADR se conservan como historia; no reflejan el framework actual.

| ADR | Decisión | Estado |
|---|---|---|
| [ADR-001](ADR-001-monolito-modular.md) | Monolito Modular con Adaptadores Selectivos | Aceptada (núcleo aún vigente, ver ADR-0005) |
| [ADR-002](ADR-002-plugins-embebidos.md) | Plugins embebidos in-process vía entry_points | Aceptada (vigente, ADR-0005) |
| [ADR-003](ADR-003-deepseek-default.md) | DeepSeek como LLM por defecto + difficulty routing | Aceptada (config, no contrato) |
| [ADR-004](ADR-004-memoria-3-capas.md) | Memoria en 3 capas + proveedor de embeddings configurable | Aceptada (vigente) |
| [ADR-005](ADR-005-migraciones-por-plugin.md) | Migraciones Alembic por plugin | Aceptada (vigente) |
| [ADR-006](ADR-006-colas-debounce-idempotencia.md) | Redis Stream + debounce ZSet + idempotencia | Aceptada (vigente, ADR-0006/0007) |
| [ADR-007](ADR-007-panel-ssr-formgenerator.md) | Panel SSR Jinja2 + FormGenerator desde Pydantic | Aceptada (vigente) |
| [ADR-008](ADR-008-single-tenant-sdd-vivo.md) | Single-tenant por despliegue + SDD vivo | **SUPERSEDED por ADR-0001** (multi-tenant pooled + RLS) |

Para decisiones nuevas usa la serie `docs/adr/` (se crea con `/adr`).