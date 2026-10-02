---
name: release
description: >
  Publica una versión de agent-core en producción: PR de release develop → main, despliegue
  de producción en Coolify, tag vX.Y.Z y verificación. Usar SOLO cuando el usuario diga
  "vamos a desplegar a producción", "release", "publicar la versión" o "subir a producción".
argument-hint: "<major|minor|patch>"
---

Release de agent-core con incremento `$0` (si no viene, proponerlo según las reglas de
semver de abajo y confirmarlo con el usuario).

A producción se llega **solo** por una release: PR de `develop` a `main`. La app de
producción despliega `main`. Lo que no pasó por test (`desplegar-test-agent-core`) no
entra.

## Versión: una sola fuente

La versión vive en tres lugares que tienen que coincidir: `pyproject.toml`
(`version = "X.Y.Z"`), el encabezado `## vX.Y.Z — <título> (AAAA-MM-DD)` de
`CHANGELOG.md` y el tag `vX.Y.Z` sobre el merge en `main`.

> Deriva conocida (2026-10-01): el CHANGELOG llega a `v0.7.0`, `pyproject.toml` dice
> `0.3.0` y solo existen los tags `v0.2.0` y `v0.3.0`. La primera release con esta skill
> usa la siguiente versión sobre la más alta ya usada (`v0.8.0` o mayor) y alinea las tres
> fuentes.

Semver:
- **major**: cambia un contrato de `packages/contracts` (requiere ADR) o algo rompe a quien
  integra la API (`/api/v1`, webhooks, CLI).
- **minor**: agrega tools, tablas, endpoints, vistas o packs.
- **patch**: correcciones sin cambio de superficie.

## Checklist (todo en verde, o no hay release)

1. **CI verde en `develop`**: ruff, migrar y sembrar, `pytest -q`, integración
   (`AGENT_CORE_INTEGRATION=1`), gate de evals (`"06,07,08,21,22,23,24,25,26"`) y gitleaks.
   El nightly (`evals-nightly.yml`) sin regresiones nuevas.
2. **Alcance**: `git log --oneline origin/main..origin/develop` y
   `git diff --stat origin/main..origin/develop`. Ningún 🔴 abierto en lo que entra.
3. **Subagente `auditor-multitenant`** sobre ese diff: sin hallazgos de severidad alta.
4. **Subagente `eval-runner`** con el golden set completo: sin regresiones frente a la
   release anterior.
5. **Migraciones** del rango:
   `git diff --name-only origin/main..origin/develop -- alembic/versions 'packages/*/alembic/versions'`.
   Cada una se aplicó y revirtió en local (skill `db-migration`), corre en test y deja
   test con 0 tablas con RLS sin `FORCE`.
6. **OpenAPI**: `uv run python scripts/export_openapi.py` sin diff en `docs/openapi.json`. Si
   la API cambió, los tipos del panel (`solvershub-panel`) se regeneran en su propio PR.
7. **Variables de entorno nuevas** cargadas en Coolify **antes** del deploy, en la app, el
   worker y el beat de producción (`docs/despliegue-coolify.md`, sección de variables).
8. **CHANGELOG**: las secciones "Sin versión" del rango se consolidan bajo
   `## vX.Y.Z — <título> (AAAA-MM-DD)`, con qué cambió y **qué tiene que hacer el
   operador** (variables, pasos manuales, cambios en Meta o Wompi). Bump en `pyproject.toml`.
9. **Backup** de la BD de producción antes de migrar, mientras no exista PITR (HU-ARCH-03).
   Lo corre el usuario:
   `docker exec huvhzaxim2notqkcxqa5930a pg_dump -U postgres -Fc agent_core > agent_core-$(date +%F).dump`.
10. **Rollback escrito en el PR**, concreto:
    - `git revert -m 1 <merge>` en `main` y redeploy;
    - `alembic downgrade` de las migraciones del rango, en orden inverso (lista explícita);
    - `agent-core agent rollback <tenant>` si cambió un AgentSpec o un playbook.

## Despliegue

Cada paso es visible para terceros o toca producción: **confirmar con el usuario antes de
cada uno**. El hook `guards.py` pide confirmación ante cualquier comando sobre los UUID de
producción, y el token de Coolify lo da el usuario en el momento (no se guarda).

1. PR de release:
   `gh pr create --base main --head develop --title "release: vX.Y.Z — <título>"`, con el
   alcance, las migraciones, las variables nuevas y el rollback.
2. Merge del PR (`gh pr merge <n> --merge`) cuando el CI de `main` esté verde.
3. Deploy de producción por la API de Coolify (`/api/v1/applications/<uuid>/start`):
   app `ggzjdyc7zsemt6kzafx5rg7m`, y si cambió código de tareas, worker
   `wv6kycjeae0lmfj11dprkasq` y beat `hwh7s5gscnn1k3yqf88xceja`. En Coolify la app de
   producción todavía se llama `agent-core-test`: guiarse por el UUID.
4. Verificación:
   - `docker ps --format '{{.Names}}\t{{.Status}}' | grep ggzjdyc` → `Up … (healthy)`;
   - `curl -s https://app.solvershub.site/healthz` → `{"status":"ok"}`;
   - migraciones de producción al head (el usuario corre el `select version_num` de cada
     `alembic_version*` contra `huvhzaxim2notqkcxqa5930a`);
   - checklist de producción en el panel: `rls_forced` en verde.
5. Tag sobre el merge en `main`:
   `git tag -a vX.Y.Z <merge> -m "vX.Y.Z — <título>" && git push origin vX.Y.Z`.

## Después: 24 h de observación

El "canary" de ADR-0016 es este gate más un tenant de staging y el rollback en un paso;
no hay reparto de tráfico. Vigilar contra la línea base de la release anterior:
- tasa de escalamiento;
- costo por conversación (Costos LLM);
- errores de tool y packs auto-deshabilitados (salud de plugins);
- webhooks rechazados o en la bandeja de fallos;
- latencia de respuesta.

Cualquiera peor que la línea base → rollback, sin discusión.

## Prohibido

- Editar prompts, AgentSpec o datos directamente en la BD de producción: se versiona en el
  panel y se activa o revierte con `agent-core agent activate|rollback`.
- Desplegar a producción desde `develop` o sin pasar por test.
- Desplegar un viernes después del mediodía sin una razón escrita.
