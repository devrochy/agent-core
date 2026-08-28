# agent-core

Framework para construir **asistentes IA empresariales** como **monolitos modulares con plugins embebidos**.

Un asistente = un despliegue de `agent-core` + los plugins que necesita. Los plugins son paquetes
Python que corren **en el mismo proceso** que el core (cero latencia de red, cero microservicios).

```
git clone <este-repo> mi-asistente
cd mi-asistente
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
agent-core init                          # wizard → .env + sdd/ + BD + admin
docker-compose up -d                     # postgres + redis + api + celery
# → http://localhost:8000/admin

agent-core plugin install mod-crm-engine # descarga, migra, expone en el panel
agent-core doctor                        # si algo no arranca, empieza por aquí
```

`init` elige puertos libres si los tuyos están ocupados y aísla cada asistente en su
propio proyecto Docker, así que puedes tener varios clones conviviendo en la misma
máquina.

## Qué incluye el core

| Capacidad | Implementación |
|---|---|
| LLM agnóstico | DeepSeek (default) · Anthropic · OpenAI · cualquier API openai-compatible |
| Difficulty routing | `deepseek-chat` (económico) ↔ `deepseek-reasoner` (razonamiento) automático |
| Memoria en 3 capas | Redis (working) · PostgreSQL (entity) · pgvector (semántica/RAG) |
| Plugins | Paquetes pip detectados por `entry_points`, ciclo de vida completo, auto-disable en error |
| MCP Host interno | Los plugins exponen tools estilo MCP; el core las traduce al function calling del LLM activo |
| Canales | WhatsApp (Meta Cloud API) incluido; adaptadores por proveedor para Telegram/Instagram futuros |
| Conexiones de canal | Credenciales gestionadas en el panel (`/admin/conexiones`), cifradas con Fernet y verificadas contra Meta antes de guardar; webhook y envío resuelven desde BD con fallback al `.env` |
| Panel admin | SSR Jinja2 + Bootstrap 5; formularios auto-generados desde los schemas Pydantic de cada plugin; el menú de cada plugin se agrupa bajo el nombre del módulo |
| Colas y cron | Redis Stream (mensajes) + Celery/Beat (`@register_cron`) |
| Robustez | Idempotencia 3 capas · debounce · guardrails · kill-switch 3 niveles · escalamiento a humano |

## Documentación

| Documento | Contenido |
|---|---|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Instalación desde cero y solución de problemas |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | Cómo se hace un cambio: planificar → ejecutar → verificar |
| [docs/FIRST-ASSISTANT.md](docs/FIRST-ASSISTANT.md) | Tu primer asistente en 15 pasos |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Arquitectura: Monolito Modular con Adaptadores Selectivos |
| [docs/PLUGIN-CONTRACT.md](docs/PLUGIN-CONTRACT.md) | La "Biblia" para desarrollar plugins |
| [docs/MONOLITH-MODEL.md](docs/MONOLITH-MODEL.md) | Por qué los plugins son código embebido, no servicios |
| [docs/SDD-TECH-STACK.md](docs/SDD-TECH-STACK.md) | Decisiones de stack + mapa de portabilidad a TypeScript |
| [docs/HU/](docs/HU) | Historias de usuario |
| [docs/ADR/](docs/ADR) | Architectural Decision Records |
| [docs/BACKLOG.md](docs/BACKLOG.md) | Deuda técnica conocida, con evidencia |

## Estructura

```
core/            # El framework: plomería (gateway, orchestrator, llm, memory, mcp, admin)
cli/             # CLI: agent-core init | plugin install | configure | db | sdd
packages/        # Contratos (congelados) + packs verticales (pack-commerce, pack-calendar)
sdd/             # System Design Document VIVO del asistente (generado por `init`)
docs/            # Documentación del framework
evals/           # Golden scenarios (assertions determinísticas)
tests/           # Tests de arranque y de los módulos puros del core
```

## Licencia

MIT
