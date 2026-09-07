# Multi-tenant en agent-core

## Misión / visión

**Un solo código base sirve a todos los clientes.** agent-core nació como un
framework single-tenant ("un asistente = un despliegue", cada cliente con su
código); hoy la misión es la inversa: **cada cliente es un tenant** dentro del
mismo despliegue, aislado por `tenant_id` + RLS (ADR-0001). No se escribe código
por cliente: la personalización se resuelve en este orden — configuración →
pack → plugin del tenant → topología de despliegue. Mismo binario siempre.

Consecuencias:

- **Alta de un cliente = configuración.** `agent-core tenant create` (o el panel
  `/admin/tenants/nuevo`) deja listo a un tenant: AgentSpec, canales, packs,
  admin. Cero código, cero scripts por cliente.
- **El framework es neutro.** No trae datos ni documentación de clientes reales.
  Todo lo específico de un tenant vive en `TENANTS_ROOT/<slug>/` (gitignored).
- **Escalar = subir de tenant**, no de repos. Los tests de aislamiento son
  genéricos: crecer en tenants nunca exige añadir tests.

## Qué es un tenant

Un tenant es un cliente con su asistente. En la BD:

| Qué | Tabla | Notas |
|---|---|---|
| El cliente | `tenants` | sistema, sin RLS |
| Su asistente | `agent_specs` | persona, tono, playbooks, packs (un activo por tenant) |
| Su identidad | `app_settings` | nombre, negocio, idioma, BYOK, kill-switch |
| Su canal | `channel_connections` | credenciales cifradas (Fernet) |
| Su vertical | `plugin_configs` + enablement | config del pack por tenant |
| Acceso del admin | `tenant_user` | quién opera el tenant en el panel |

Aislamiento: **toda** tabla de negocio lleva `tenant_id uuid not null`, RLS con
política de aislamiento e índice compuesto que empieza por `tenant_id`
(invariante #1 de `CLAUDE.md`). Un tenant nunca ve filas de otro.

## `TENANTS_ROOT` — los artefactos de cada tenant viven fuera del código

Los datos y la documentación específica de cada cliente no van al repo del
framework. Viven en `TENANTS_ROOT` (default `./tenants`, gitignored):

```
tenants/
  <slug>/
    catalog.json     # datos del tenant importables a la BD (seed) — formato del pack
    docs/            # documentación específica del tenant (README, config, changelog)
    evals/           # golden set del tenant (spec.json + scenarios/) para `eval-runner --tenant`
```

- `TENANTS_ROOT` se configura en `.env` (`core/config.py`). Por defecto es
  relativo al repo; puede apuntar a un directorio compartido.
- La documentación del **framework** vive en `docs/`; la de cada **tenant**, en
  `TENANTS_ROOT/<slug>/docs/`. No se mezclan.

## Flujo

```
agent-core init              # bootstrap del despliegue (una vez por repo/BD)
  └─ agent-core tenant create <cliente>   # alta (CLI o panel)
       └─ agent-core tenant seed <slug>   # importa catalog.json del tenant
            └─ evals --tenant <slug>      # golden set del tenant antes de publicar
```

Un despliegue nuevo = `git clone` + `init` (BD vacía) + `tenant create` por cada
cliente. No hay "otro repo por cliente": hay otro **tenant**.

## Ciclo de vida de un tenant

Un tenant nace `active` y puede transitar a `suspended` y a `eliminado`:

| Estado | Efecto | Cómo |
|---|---|---|
| `active` | Procesa mensajes normalmente | alta (`tenant create`) o reactivación |
| `suspended` | Deja de procesar mensajes (webhook y consumer lo rechazan), **conserva sus datos** y sigue visible en el panel | `tenant deactivate` (CLI) o botón en `/admin/tenants` |
| eliminado | Borrado físico **irreversible** de todo su dato (core + packs + Redis + fila `tenants`) | `tenant purge` o `/admin/tenants` (modal) |

**Suspensión** (`tenant deactivate`): reversible (`tenant activate`). Útil para
bajas temporales, impago, o incidentes. La enforcement es defensa en profundidad:
el webhook no encola (`core/gateway/webhooks.py`) y el consumer descarta eventos
ya encolados (`core/orchestrator/consumer.py`) vía `core.tenants.is_tenant_active`.

**Eliminación** (`tenant purge`): exige confirmar exactamente la frase
`ELIMINAR <slug>` y rol `master`. Es la **excepción deliberada a la invariante #8**
(sin borrado físico): el ciclo de vida del tenant la requiere (tipo GDPR /
right-to-erasure). La función SQL `purge_tenant(tid)` (SECURITY DEFINER, dueño,
transaccional) descubre dinámicamente todas las tablas con `tenant_id` — core y
packs — y las vacía; además se limpia el estado Redis. Destruye también el
historial inmutable (`usage_ledger`, `audit_log`) del tenant.

## Referencias

- ADR-0001 multi-tenant pooled + RLS (`docs/adr/0001-multitenant-pooled-rls.md`)
- ADR-0012 contexto de tenant (auth/RLS) · ADR-0014 AgentSpec/playbooks
- ADR-0019 enablement por packs · ADR-0018 numeración por tenant
- `core/onboarding.py` — el servicio de alta (usado por CLI y panel)
- `cli/commands/tenant.py` — comandos `tenant create|list|seed|docs`