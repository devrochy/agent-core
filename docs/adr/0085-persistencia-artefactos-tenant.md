# ADR-0085 — Persistencia de los artefactos de tenant en despliegue

- **Estado:** aceptado
- **Fecha:** 2026-09-30
- **Relacionado:** ADR-0026 (override de playbooks), ADR-0054 (flow por tenant), ADR-0073 (prefijo de ruta)

## Contexto

Cada tenant tiene artefactos en **filesystem** bajo `TENANTS_ROOT` (default `/app/tenants`,
`core/onboarding.py:42-53`):

- `flow.json` — flujo de atención autoritativo del tenant (ADR-0054,
  `core/agents/flow.py:108-113,225-231`).
- `playbooks/*.md` — override de playbooks sobre pack/core (ADR-0026,
  `core/agents/playbooks.py:87-124`).
- `catalog.json`, `docs/`, `evals/` — catálogo sembrable y golden del tenant.

En **Coolify el contenedor no monta volúmenes**: verificado en test, el app
`agent-core-test` (`tr0vp…`) tiene `Mounts = []` y `/app/tenants/solvershub` solo contiene
`docs/` (creado en runtime). Consecuencia: **todo `flow.json`/playbook/catálogo que se cree
o siembre en el despliegue se pierde en el próximo redeploy**. Solo lo que vive en la **BD**
sobrevive (p. ej. `brand_voice` en `AppSettings`; `AgentSpec` en `agent_specs`).

Esto ya rompió el despliegue antes: el `flow.json` del tenant `solvershub` no llega al
contenedor, así que `resolve_flow` cae al flujo del pack y las secciones propias del tenant
no aplican.

## Decisión

Persistir `TENANTS_ROOT` con un **volumen** en el despliegue (Coolify), y dejar la migración
a BD como decisión posterior:

1. **Coolify**: montar un volumen persistente en `TENANTS_ROOT` para **cada** contenedor que
   lee/escribe artefactos de tenant: app (`api`), `worker` y `beat`. En `docker-compose.yml`
   añadir el volumen equivalente.
2. **Arranque**: el entrypoint/onboarding asegura la existencia de `TENANTS_ROOT` y de los
   subdirs por tenant; los seeds y el panel escriben en ese volumen.
3. **Backup**: incluir `TENANTS_ROOT` en el respaldo (hoy vive dentro del contenedor).
4. **Follow-up (REQ)**: evaluar mover `flow` y playbooks **a BD** (versionados) para tener
   una sola fuente de verdad y evitar dependencia de filesystem; se hará con ADR propio si
   se decide.

## Alternativas consideradas

| Opción | Por qué no (ahora) |
|---|---|
| No persistir (estado actual) | Los artefactos se pierden en cada redeploy; rompe flujos por tenant |
| Hornear `tenants/` en la imagen | El repo NO contiene datos de clientes (invariante); multi-tenant en un binario |
| Mover todo a BD ya | Más alcance (cambia loaders de flow/playbooks y su versionado); se difiere |
| Bind-mount del repo host | Atado a la máquina; Coolify remoto no lo tiene |

## Consecuencias

**Positivas:** los artefactos de tenant sobreviven redeploys; `flow`/overrides por tenant
funcionan en test y prod; respaldo posible.

**Negativas / lo que aceptamos pagar:** un volumen que administrar por entorno; sigue
habiendo dos fuentes (BD para spec/brand, filesystem para flow/playbooks); el volumen debe
montarse en app+worker+beat o se desincroniza.

**Qué haría que revisáramos esto:** que se necesite versionar/rollback de flow y playbooks
como el `AgentSpec` → mover a BD (follow-up).

## Impacto en el código / despliegue

- `docker-compose.yml` (volumen `tenants-data` en api/worker/beat).
- `docs/despliegue-coolify.md` (montaje del volumen en Coolify + backup).
- `core/onboarding.py` (asegurar dirs) y `scripts/entrypoint.sh` (mkdir de `TENANTS_ROOT`).
