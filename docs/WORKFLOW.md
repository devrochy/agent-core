# Cómo se hace un cambio en agent-core

Este documento define el ciclo de trabajo. Es corto a propósito: si no cabe en la
cabeza, no se sigue.

La regla de fondo: **primero se planifica, después se ejecuta.** El plan no es
burocracia, es lo que evita descubrir a mitad de camino que el cambio toca el
contrato de plugins o una migración.

---

## El ciclo, en cinco pasos

### 1. Encuadre

Clasifica el cambio antes de abrir un editor. La clasificación decide el nivel de
verificación y qué documento hay que cerrar al final.

| Tipo | Qué toca | Gate | Documento dueño |
|---|---|---|---|
| `doc` | `docs/`, `README.md`, `sdd/templates/` | N1 | El propio documento |
| `plugin` | `plugins/**` | N2 | `PLUGIN-CONTRACT.md` si cambia el contrato |
| `core` | `core/**`, `cli/**` | N2 | `ARCHITECTURE.md` si cambia el flujo |
| `runtime` | `core/gateway`, `core/orchestrator`, `core/llm`, `alembic/` | N3 | `ARCHITECTURE.md` + ADR si es estructural |
| `infra` | `Dockerfile`, `docker-compose.yml`, `scripts/`, `Makefile` | N3 | `INSTALLATION.md` |

### 2. Plan escrito

Antes del código, cuatro respuestas: **qué cambia**, **qué archivos**, **qué se puede
romper**, **cómo se verifica**.

Además:

- Decisión **estructural** (afecta al patrón, no a la implementación) → **ADR nuevo**
  en `docs/ADR/`, con el formato existente: Contexto / Decisión / Justificación /
  Consecuencias con `(+)` y `(−)`.
- Funcionalidad **de cara al usuario** → **HU nueva** en `docs/HU/` con criterios de
  aceptación verificables.

**Regla dura:** si el cambio contradice un ADR aceptado, no se ejecuta hasta escribir
el ADR que lo supersede. Un ADR no se edita en silencio; se reemplaza.

### 3. Ejecución

```bash
git checkout -b <tipo>/<descripcion-corta>     # fix/costos-anthropic
```

Un tema por rama. Cambios mínimos. Commits en español siguiendo Conventional Commits
(`feat:`, `fix:`, `docs:`, `chore(infra):`), que ya es la práctica del repo.

**Nunca se editan a mano los `sdd/*.md`**: son generados. Se edita el template
(`sdd/templates/*.md.j2`) o el estado (`sdd/.state.json` vía CLI).

### 4. Verificación

No se da por terminado nada que no haya pasado su gate. Si un paso se salta, se dice
explícitamente en vez de darlo por hecho.

**N1 · doc**

Revisa el render. Si tocaste un template: `agent-core sdd update`.

**N2 · código**

```bash
make lint     # ruff sobre core, cli, tests, evals, scripts
make test     # 72 tests, sin BD ni red ni API key
python -m compileall core cli plugins
```

**N3 · runtime e infra** — todo lo de N2, más:

```bash
make up
agent-core doctor                      # debe salir en verde
curl -s localhost:$APP_HOST_PORT/healthz
```

Después, prueba manual del flujo que tocaste en el panel. Y antes de desplegar:

```bash
make evals    # golden scenarios; requiere LLM_API_KEY y el stack arriba
```

Los evals van **antes de cada deploy, no en cada commit** (criterio del propio
`evals/runner.py`). Si no hay API key, se reporta como no ejecutado — no se da por
bueno.

**Cambios que tocan el arranque** (puertos, migraciones, `docker-compose.yml`,
`entrypoint.sh`, `init`) tienen una verificación propia, porque el flujo de
instalación *es* el producto:

```bash
git clone <repo> /tmp/verificar && cd /tmp/verificar
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/agent-core init --non-interactive --name "Verificar"
docker compose up -d && .venv/bin/agent-core doctor
```

Debe completarse sin intervención manual, en una máquina que ya tenga puertos
ocupados y volúmenes de instalaciones anteriores.

### 5. Cierre documental

Antes del merge, en este orden:

1. Actualiza **el documento dueño** del tema (tabla de abajo). Solo ese: los demás
   enlazan.
2. Marca los criterios cumplidos en la HU (`- [ ]` → `- [x]`).
3. Si cambió el estado del asistente (plugin, tono, LLM): `agent-core sdd update`.
4. `git merge --no-ff` a `main`.

---

## Un tema, un dueño

Sin esta tabla la documentación se duplica y las copias divergen. Cada tema tiene un
documento que manda; el resto enlaza en vez de repetir.

| Tema | Dueño | El resto |
|---|---|---|
| Por qué de una decisión estructural | `docs/ADR/` | Enlazan al ADR |
| Flujo runtime de un mensaje | `docs/ARCHITECTURE.md` | Enlazan |
| Cómo escribir un plugin | `docs/PLUGIN-CONTRACT.md` | Enlazan |
| Instalar, arrancar y diagnosticar | `docs/INSTALLATION.md` | Enlazan |
| Proceso de trabajo | `docs/WORKFLOW.md` (este) | `CLAUDE.md` resume |
| Qué se pide construir | `docs/HU/` | — |
| Estado del asistente desplegado | `sdd/.state.json` → `sdd/*.md` | **Generado. Nunca a mano** |
| Reglas para agentes IA | `CLAUDE.md` | `.opencode/agents/*` enlazan |
| Deuda técnica conocida | `docs/BACKLOG.md` | — |

**Corolario para los templates SDD:** describen *este asistente*, no el framework.
Lo que sea del framework se parametriza desde `.state.json` o se enlaza a `docs/`.
Un valor del framework copiado a mano en un `.j2` miente en cuanto alguien toca el
`.env`.

---

## Qué hacer con lo que encuentras y no arreglas

A `docs/BACKLOG.md`, con la evidencia (archivo y línea). Un hallazgo sin anotar se
vuelve a descubrir dentro de seis meses.
