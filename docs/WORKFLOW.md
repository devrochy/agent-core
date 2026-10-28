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
.venv/bin/agent-core tenant create --non-interactive --name "Cliente de prueba" --packs pack_commerce
```

Debe completarse sin intervención manual, en una máquina que ya tenga puertos
ocupados y volúmenes de instalaciones anteriores. El `tenant create` final prueba
que una instalación limpia puede dar de alta clientes de verdad.

### 5. Cierre documental

Antes del merge, en este orden:

1. Actualiza **el documento dueño** del tema (tabla de abajo). Solo ese: los demás
   enlazan.
2. Marca los criterios cumplidos en la HU (`- [ ]` → `- [x]`).
3. Si cambió el estado de un tenant (plugin, tono, LLM): `agent-core sdd update`
   (o anótalo en `TENANTS_ROOT/<slug>/docs/`).
4. `git merge --no-ff` a `main`.

---

## CI: qué corre en cada evento (ADR-0120)

| Evento | Nivel | Qué corre |
|---|---|---|
| PR → develop | rápido | ruff, basedpyright, import-linter, unitarios, gitleaks sobre los commits de la PR y, si la PR toca `core/`, `packages/`, `plugins/`, `alembic/`, `cli/`, `tests/integration/`, `pyproject.toml`, `uv.lock` o `.github/`, el subconjunto `pytest tests/integration -m aislamiento` |
| push → develop (post-merge) | completo | lo anterior, más la integración completa, el gate de evals y journeys y gitleaks sobre toda la historia |
| PR → main / push → main | completo | lo del push a develop, más el drift de `docs/openapi.json` |

- **Jobs en paralelo.** `nivel` (segundos) decide el nivel; `rapido`, `integracion` y `evals-replay` corren a la vez. Con un solo runner igual hacen cola; por eso conviene tener `solvershub-ci-2` en línea (ver abajo). Los unitarios corren con `pytest -n 4`: un `parametrize` con ids aleatorios (`uuid4()`) rompe la colección en xdist; usa `uuid5` o ids fijos.
- **Duraciones de la integración.** Cada corrida sube el artifact `integracion-junit` y imprime los 40 tests más lentos (`--durations=40`).
- **Despliegue a test.** Una PR verde en develop **no** garantiza que pase la integración completa. No se despliega a test hasta que el CI del push a develop esté en verde.
- **Tests nuevos de aislamiento.** Un test de integración nuevo que proteja el aislamiento entre tenants (RLS, `tenant_id` explícito, IDOR, webhooks multi-tenant) lleva `pytest.mark.aislamiento`.
- **Postgres del CI.** Es la imagen de producción (`docker/postgres/Dockerfile`), publicada en GHCR por `ci-postgres-image.yml`. Si cambia el Dockerfile, ese workflow la republica; para refrescar la imagen base, "Run workflow" en Actions. **Arranque:** `workflow_dispatch` solo existe cuando el workflow ya está en `main`, así que la primera imagen se publica a mano desde el host del runner (la etiqueta `source` vincula el paquete al repo para que el `GITHUB_TOKEN` del CI pueda bajarla):

  ```bash
  gh auth refresh -s write:packages
  gh auth token | docker login ghcr.io -u devrochy --password-stdin
  docker build --pull --label org.opencontainers.image.source=https://github.com/devrochy/agent-core -t ghcr.io/devrochy/solvershub-postgres:pg17-postgis docker/postgres
  docker push ghcr.io/devrochy/solvershub-postgres:pg17-postgis && docker logout ghcr.io
  ```

### Segundo runner en el mismo host

Los services usan puertos dinámicos, así que varios runners pueden compartir la
máquina. Para registrar otro, con el token de *Settings → Actions → Runners → New
self-hosted runner*:

```bash
mkdir ~/actions-runner-2 && cd ~/actions-runner-2
# Descargar y descomprimir el mismo paquete del runner que usa ~/actions-runner
./config.sh --url https://github.com/devrochy/agent-core --token <TOKEN> \
  --name solvershub-ci-2 --labels self-hosted,linux,x64 --unattended
sudo ./svc.sh install && sudo ./svc.sh start
```

El host ya tiene otros runners (`actions-runner-apk`, `actions-runner-panel`) y los
contenedores de Coolify de test. Antes de sumar un tercero, conviene vigilar la CPU
durante una corrida.

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
| Estado del tenant desplegado | `TENANTS_ROOT/<slug>/docs/` + `sdd/.state.json` | **Generado. Nunca a mano** |
| Reglas para agentes IA | `CLAUDE.md` | `.opencode/agents/*` enlazan |
| Deuda técnica conocida | `docs/BACKLOG.md` | — |

**Corolario para los templates SDD:** describen *este despliegue*, no el framework.
Lo que sea del framework se parametriza desde `.state.json` o se enlaza a `docs/`.
Lo que sea de un cliente concreto va a `TENANTS_ROOT/<slug>/docs/`, no al repo del
framework. Un valor del framework copiado a mano en un `.j2` miente en cuanto
alguien toca el `.env`.

---

## Qué hacer con lo que encuentras y no arreglas

A `docs/BACKLOG.md`, con la evidencia (archivo y línea). Un hallazgo sin anotar se
vuelve a descubrir dentro de seis meses.
