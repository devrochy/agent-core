# agent-core

Framework de asistentes IA: **monolito modular con plugins embebidos**. FastAPI +
SQLAlchemy async + Postgres/pgvector + Redis + Celery. Los plugins son paquetes pip
que corren **en el mismo proceso** que el core, detectados por `entry_points`.

**Es una plantilla instalable**: cada asistente es un clon de este repo. El flujo
`git clone` → `pip install -e .` → `agent-core init` → `docker compose up` *es el
producto*. Si un cambio lo rompe, rompe todo.

## Antes de tocar código

Lee **[docs/WORKFLOW.md](docs/WORKFLOW.md)**: clasificar → planificar → ejecutar →
verificar → cerrar documentación. Primero se planifica, después se ejecuta.

## Comandos

Todo asume el venv del proyecto activo (`source .venv/bin/activate`). Fuera de él
`python` puede no existir y `agent-core` puede resolver a otra instalación.

```bash
make install          # pip install -e ".[dev]"
make test             # pytest — sin BD, sin red, sin API key
make lint             # ruff sobre core, cli, tests, evals, scripts
make up / make down   # stack completo
make migrate          # == agent-core db migrate (core + plugins)
make evals            # golden scenarios (antes de deploy, no en cada commit)

agent-core doctor     # primer paso ante cualquier fallo de arranque
agent-core init --non-interactive --name "X"    # init automatizable
```

## Verificación según qué tocas

| Tocas | Verifica con |
|---|---|
| `docs/`, `sdd/templates/` | Revisar render; `agent-core sdd update` si es template |
| `core/`, `cli/`, `plugins/` | `make lint` + `make test` + `compileall` |
| `gateway`, `orchestrator`, `llm`, `alembic/`, infra | Lo anterior + `make up` + `agent-core doctor` + probar el flujo |
| Arranque (puertos, migraciones, compose, entrypoint, init) | Clonar en limpio y arrancar de cero — ver WORKFLOW.md |

## Reglas duras

- **`sdd/*.md` son generados.** Nunca se editan a mano: se edita
  `sdd/templates/*.md.j2` o el estado vía CLI.
- **Los plugins no importan `core.orchestrator.*`.** Se comunican por el `MCPHost`
  y por eventos. Ver [docs/PLUGIN-CONTRACT.md](docs/PLUGIN-CONTRACT.md).
- **Un ADR aceptado no se edita en silencio**: se escribe el que lo supersede.
- **Cada plugin tiene su propio árbol Alembic** con `version_table` separada. Las
  migraciones van por `core/db/migrations.py`, que es el único camino: lo usan `init`,
  el CLI, `make migrate` y el entrypoint del contenedor.
- **Un tema, un dueño** en documentación. La tabla está en WORKFLOW.md. No dupliques:
  enlaza.
- **Lo que encuentres y no arregles** va a [docs/BACKLOG.md](docs/BACKLOG.md) con
  archivo y línea.

## Trampas conocidas del entorno

- PostgreSQL aplica `POSTGRES_USER/PASSWORD/DB` **solo al crear el volumen**. Un
  `pgdata` heredado conserva las credenciales viejas y el fallo aparece como si fuera
  de migraciones. `agent-core doctor` lo detecta.
- Sin `COMPOSE_PROJECT_NAME`, Docker nombra el proyecto por la carpeta y dos clones
  homónimos comparten volumen.
- `docker compose` exige un `.env`; lo genera `agent-core init`.

## Dónde está cada cosa

```
core/       gateway (webhooks, envío) · orchestrator (consumer, debounce, loop)
            llm · memory (redis/postgres/pgvector) · mcp (contrato) · admin (SSR) · db
cli/        init · doctor · plugin · db · configure · sdd · preflight (chequeos)
plugins/    mod_crm_engine (referencia, completo) · mod_google_calendar (mínimo)
tests/      lógica de arranque y módulos puros del core
docs/       WORKFLOW · INSTALLATION · ARCHITECTURE · PLUGIN-CONTRACT · ADR/ · HU/ · BACKLOG
```
