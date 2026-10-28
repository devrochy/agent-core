# ADR-0120 — CI por niveles: rápido en las PRs a develop, completo hacia main

- **Estado:** aceptado
- **Fecha:** 2026-10-08

## Contexto
- Una PR tardaba entre 8 y 40 min en tener resultado.
- El job `gate` dura unos 6 min: integración 3 min 10 s, pip sin caché 41–90 s y `apt-get install postgis` 30–45 s en cada corrida.
- Lo que más pesa es la cola: hay **un solo runner** self-hosted, y cada PR y cada push a develop corren uno detrás de otro, dos veces por cambio. Los puertos fijos (55432/56379) impedían un 2º runner en el host.
- El CI usaba `pgvector:pg16`; producción usa pg17 + PostGIS (`docker/postgres/Dockerfile`).

## Decisión
El CI corre en dos niveles según el destino. El nivel completo se reserva para lo que ya entró a develop y para lo que va hacia main.

| Evento | Nivel | Qué corre |
|---|---|---|
| PR → develop (o cualquier base que no sea main) | rápido | ruff, basedpyright, import-linter, `pytest -q`, gitleaks solo sobre los commits de la PR y, si la PR toca rutas que cubre la integración, `pytest tests/integration -m aislamiento` |
| push → develop, PR → main, push → main | completo | lo anterior, más la integración completa, el gate de evals y journeys, gitleaks sobre toda la historia y, hacia main, drift de `docs/openapi.json` |

Detalle:
- **Marker `aislamiento`.** Agrupa los tests de integración que protegen el invariante 1: RLS forzada, aislamiento entre tenants, webhooks multi-tenant y por slug, IDOR, filtro `tenant_id` explícito, RBAC y ciclo de vida del tenant. Un test nuevo de aislamiento lleva este marker.
- **Rutas relevantes** (`RUTAS_INTEGRACION` en `ci.yml`): `core/`, `packages/`, `plugins/`, `alembic/`, `cli/`, `tests/integration/`, `tests/conftest.py`, `alembic.ini`, `scripts/init_db.py`, `scripts/install_plugins.sh`, `pyproject.toml`, `uv.lock`, `.github/` y `docker/postgres/`.
- **Imagen de Postgres.** El Postgres del CI es la misma imagen que producción, publicada en GHCR (`ci-postgres-image.yml`).
- **Puertos dinámicos.** Los services usan puertos de host dinámicos, para que varios runners puedan compartir la máquina.
- **Instalación.** Se hace con `uv pip` y una caché persistente en el host (`.github/actions/preparar-entorno`), compartida con `evals-nightly.yml`.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|
| Filtro por rutas y la integración completa en la PR | 36 de las últimas 40 PRs tocaban rutas relevantes: casi nunca se saltaría |
| Toda la integración post-merge | El aislamiento entre tenants (invariante 1) quedaría sin verificar antes del merge |
| Paralelizar la integración con pytest-xdist | Los tests comparten un loop de sesión y el estado de la base: tocaría reescribir fixtures |
| Runners hospedados por GitHub | El repo es privado en el plan gratuito (minutos limitados). Además el runner self-hosted ya existe y tiene recursos de sobra (8 núcleos, 62 GB) |

## Consecuencias
**Positivas:**
- La PR a develop baja de ~6 min (más la cola) a ~1 min sin integración y a ~2–2,5 min con el subconjunto.
- Con 2 runners, la cola deja de serializar las PRs.
- El CI prueba pg17 + PostGIS como producción y ya no instala PostGIS en cada corrida.

**Negativas / lo que aceptamos pagar:**
- Un fallo de integración fuera del subconjunto, o una regresión de evals, se ve **después** del merge, en el push a develop.
- Mitigación: no se despliega a test con el CI de develop en rojo (skill `desplegar-test-agent-core`). La release exige el nivel completo en verde.
- La imagen de GHCR es un artefacto más que mantener: se reconstruye cuando cambia `docker/postgres/`.

**Qué haría que revisáramos esto:**
- Más de 2 reversiones o arreglos en develop en un mes por fallos de integración que el subconjunto no detectó. En ese caso, ampliar el subconjunto o volver a la integración completa en la PR.
- Que el nivel rápido con subconjunto pase de 4 min.

## Impacto en el código
- **Workflows:** `.github/workflows/ci.yml` (reescrito: jobs `rapido` e `integracion`), `.github/workflows/ci-postgres-image.yml` (nuevo) y `.github/workflows/evals-nightly.yml`.
- **Acción compuesta:** `.github/actions/preparar-entorno/action.yml` (nueva).
- **Scripts:** `scripts/install_plugins.sh` respeta `$PIP`.
- **Tests:** marker `aislamiento` en `pyproject.toml` y en 12 archivos de `tests/integration/`.
- **Skills:** `desplegar-test-agent-core` y `release`.
- **Documentación:** `docs/WORKFLOW.md`, sección "CI".

## Adenda (2026-10-10): gate de evals en replay en las PRs a develop

El dueño la aprobó el 2026-10-10. Usa el modo grabar/reproducir de la adenda del 2026-10-10 de ADR-0119.

- **Nivel rápido:** suma el job `evals-replay`. Corre en todas las PRs a `develop`, también las de Dependabot, porque no
  necesita secretos. Hace lo siguiente:
  - levanta su propia base nueva (pg17 + PostGIS de GHCR), migrada y sembrada como al grabar;
  - corre el mismo gate que el push a develop (golden `06,07,08,21,22,23,24,25,26` y journeys `01,05` con umbral
    1.0) con `bash scripts/gate_evals.sh replay`;
  - toma las respuestas del LLM de `evals/cassettes/`, sin red y con `LLM_API_KEY` vacía.
- **Si la PR cambia un prompt, una tool o el flujo sin regrabar:** el job falla con exit 3, el diff y el comando para
  regrabar. Quien hizo el cambio regraba el gate con OK del dueño (`bash scripts/gate_evals.sh record`, unas 9 llamadas,
  ~0,01 USD) y commitea los JSON en la misma PR.
- **Nivel completo y nightly:** sin cambios. El gate del push a develop y el nightly siguen con el LLM real: son la
  medición de ADR-0119. El replay no mide el modelo, solo detecta antes del merge que el código determinista o el
  prompt cambiaron.
- **Costo:** la PR suma un job de unos 2–3 min en el runner self-hosted, en paralelo a la integración. No gasta saldo
  del proveedor.
- **Qué haría que revisáramos esto:** regrabaciones tan frecuentes que el costo o la fricción superen al de correr el
  gate en vivo en la PR, o grabaciones que no coinciden entre la máquina local y el CI.

## Adenda (2026-10-10): develop no gasta LLM

Decisión del dueño: **develop no genera costos de LLM y producción los mínimos.**

- **Push a `develop`:** el gate de evals y journeys corre en replay (job `evals-replay`, base nueva, sin
  `LLM_API_KEY`), igual que en las PRs a `develop`. Antes corría en vivo en cada merge.
- **PR de release hacia `main`:** el único punto del CI que llama al LLM real: el mismo gate (golden
  `06,07,08,21,22,23,24,25,26` y journeys `01,05`, k = 1), una vez por release. El push a `main` ya no lo repite.
- **Set completo (`evals-nightly.yml`):** sin `schedule`. Se lanza a mano (`workflow_dispatch`, `k` = 2 por
  defecto) cuando haga falta medir pass^k, por ejemplo antes de una release. Antes corría todas las noches con
  k = 4 (~0,5–0,8 USD por noche).
- **Lo que se pierde:** la señal diaria de inestabilidad del modelo y el criterio «≥ 95 % tres noches seguidas»
  (HU-SUB-12, ADR-0119): pasa a medirse en corridas a demanda antes de la release.
- **Qué haría que revisáramos esto:** una regresión del modelo que llegue a una release porque nadie lanzó la
  medición a demanda, o un cambio de proveedor o modelo (eso siempre se mide en vivo antes de activarlo).


## Adenda (2026-10-28): menos espera en el CI del backend

Medido sobre los últimos 25 runs de `ci.yml`: el job `rapido` dura 2–3,5 min; la integración, ~11 min en la PR
(`-m aislamiento`) y ~19–20 min en el push a develop (1829 tests en serie). Además, 6 de 24 runs esperaron entre
416 y 864 s a que arrancara el primer job: `solvershub-ci-2`, el segundo runner con el que contaba este ADR, llevaba
apagado desde el 2026-10-09.

- **Job `nivel`.** El cálculo del nivel y de las rutas relevantes sale de `rapido` a un job corto (checkout + diff).
  `rapido`, `integracion` y `evals-replay` arrancan a la vez, en vez de `integracion` y `evals-replay` esperar a
  `rapido`. Si lint falla, la integración ya habrá corrido: es el costo aceptado.
- **Gitleaks** usa un binario cacheado en el host (`~/.cache/ci-bin/`) en vez de bajarlo en cada corrida.
- **Unitarios con `pytest -n 4`** (pytest-xdist). Exige que la colección sea idéntica en todos los workers: los ids de
  `parametrize` no pueden usar `uuid4()`; se usan `uuid5` con nombre fijo.
- **Medición de la integración.** `--durations=40` y un junit XML subido como artifact (`integracion-junit`, 14 días).
  Con ese perfil se decide si la integración se reparte entre workers.
- **La integración sigue en serie.** Paralelizarla con xdist (una base `agent_core_gwN` por worker, copiada de la
  plantilla migrada, y una base de Redis por worker) sigue siendo la opción de mayor ahorro, pero sigue exigiendo
  aislar tenants fijos, claves de Redis globales, `TENANTS_ROOT` y los roles de `test_tenant_lifecycle`. Se decide con
  los datos del artifact; este ADR no la acepta todavía.
- **Qué haría que revisáramos esto:** la mediana de pared de una PR a develop (cola + ejecución) sigue sobre 8 min con
  dos runners en línea, o la CPU del host se satura al correr dos jobs de integración a la vez.
