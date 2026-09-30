# agent-core

Framework **multi-tenant de agentes IA**: un solo código base sirve a todos tus
clientes. Cada cliente es un **tenant** con su propio asistente (`AgentSpec`),
aislado por `tenant_id` + RLS (ADR-0001). Cero forks, cero código por cliente,
cero prompts en código.

Los plugins (packs) son paquetes Python que corren **en el mismo proceso** que
el core (cero latencia de red, cero microservicios, ADR-0005).

```
# 1. Instala la plataforma UNA vez (por despliegue)
git clone <este-repo> mi-despliegue
cd mi-despliegue
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
agent-core init                            # .env + BD + admin (bootstrap del despliegue)
docker compose up -d                       # postgres + redis + api + celery
# → http://localhost:8000/admin

# 2. Da de alta a cada cliente como tenant
agent-core tenant create                   # wizard paso a paso (o /admin/tenants/nuevo en el panel)
agent-core tenant seed <slug>              # importa TENANTS_ROOT/<slug>/catalog.json
```

Cada tenant tiene su propia identidad, tono, playbooks, packs y canales. Los
datos y la documentación de cada cliente viven en `TENANTS_ROOT/<slug>/`
(gitignored), separados del framework. Ver [docs/MULTITENANT.md](docs/MULTITENANT.md).

## Qué incluye el core

| Capacidad | Implementación |
|---|---|
| Multi-tenant | `tenant_id` + RLS en toda tabla de negocio; enablement por packs (ADR-0019) |
| AgentSpec | Personalidad del asistente por tenant en BD (ADR-0014); playbooks con allowlist |
| LLM agnóstico | DeepSeek (default) · Anthropic · OpenAI · cualquier API openai-compatible |
| Memoria en 3 capas | Redis (working) · PostgreSQL (entity) · pgvector (semántica/RAG) |
| Plugins | Paquetes pip detectados por `entry_points`, ciclo de vida completo, auto-disable en error |
| MCP Host interno | Los plugins exponen tools estilo MCP; el core las traduce al function calling del LLM activo |
| Canales | WhatsApp (Meta Cloud API) incluido; adaptadores por proveedor |
| Panel admin | SSR Jinja2 + Bootstrap 5; editor de AgentSpec, selector de tenant, wizard de alta |
| Robustez | Idempotencia 3 capas · debounce · guardrails · kill-switch · escalamiento a humano · canary/rollback |

## Documentación

| Documento | Contenido |
|---|---|
| [docs/MULTITENANT.md](docs/MULTITENANT.md) | Misión/visión multi-tenant + modelo de tenants + `TENANTS_ROOT` |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Instalación de la plataforma y solución de problemas |
| [docs/FIRST-ASSISTANT.md](docs/FIRST-ASSISTANT.md) | Tu primer tenant en 15 pasos |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | Cómo se hace un cambio: planificar → ejecutar → verificar |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Arquitectura: Monolito Modular con Adaptadores Selectivos |
| [docs/PLUGIN-CONTRACT.md](docs/PLUGIN-CONTRACT.md) | La "Biblia" para desarrollar plugins |
| [docs/HU/](docs/HU) | Historias de usuario |
| [docs/adr/](docs/adr) | Architectural Decision Records (serie actual 0001-0021) |
| [docs/BACKLOG.md](docs/BACKLOG.md) | Deuda técnica conocida, con evidencia |

## Estructura

```
core/            # El framework: plomería (gateway, orchestrator, llm, memory, mcp, admin, onboarding)
cli/             # CLI: agent-core tenant | plugin | db | init | configure | sdd | agent
packages/        # Contratos (congelados) + packs verticales (pack-commerce, pack-calendar)
tenants/         # Datos de cada cliente (gitignored): catalog.json · docs/ · evals/
docs/            # Documentación del framework (sin datos de clientes)
evals/           # Golden set genérico del framework
tests/           # Tests de arranque, módulos puros y de aislamiento multi-tenant
```

## Licencia

MIT