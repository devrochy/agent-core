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
