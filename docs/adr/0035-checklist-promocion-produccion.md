# ADR-0035 — Checklist de promoción a producción: 9 checks automáticos reales + manuales persistidos

- **Estado:** aceptado
- **Fecha:** 2026-09-20

## Contexto

El ticket pide una checklist de promoción a producción con 9 checks
automáticos que "de verdad ejecuten" (no casillas) más una checklist
manual persistida. Está escrito con vocabulario de Supabase/PostgREST
(`service_role`, "exposed schemas", pgTap) y de un bundler Next.js/Vite
(`NEXT_PUBLIC_*`) -- ninguno de los dos existe en este repo (Postgres liso
con RLS a mano, panel Jinja2 server-rendered sin bundler). Cada concepto se
tradujo explícitamente en vez de forzar un mapeo 1:1 sin sentido.

Hallazgo de research más importante: de los 9 checks, la mayoría solo dan
una respuesta con sentido corriendo contra un despliegue de PRODUCCIÓN
real (handshake de webhook contra la URL pública, `GET /{app_id}/
subscriptions` contra la app real, heartbeat del worker real, tokens
reales). El `gate` de CI corre contra Postgres/Redis efímeros y nunca
contra Meta real -- por eso "ejecutable en CI antes del deploy" se tradujo
a un script standalone pensado para el runbook de despliegue (Coolify),
no a un paso nuevo del `gate.yml` existente.

## Decisión

1. **Traducciones explícitas** (documentadas en el docstring del módulo
   nuevo, `core/gateway/meta_production_readiness.py`):
   - `service_role`/"exposed schemas" -> RLS con `FORCE` + grants de
     Postgres sobre `meta_private` (no hay PostgREST que "exponga" nada).
   - pgTap -> se reusan los tests pytest de aislamiento multi-tenant ya
     existentes, corridos por subprocess.
   - `NEXT_PUBLIC_*` -> no hay bundler; se reinterpreta como "ningún
     template interpola un campo cuyo NOMBRE sugiere un secreto"
     (comparación por componente, no substring -- ver hallazgo abajo).
2. **Hallazgo real durante la implementación**: ninguna migración de este
   repo usaba `FORCE ROW LEVEL SECURITY` -- el dueño de la tabla podía
   saltarse las policies. Se corrigió con una migración nueva
   (`0029_force_row_level_security.py`) que resuelve la lista de tablas
   DINÁMICAMENTE (`pg_class`/`pg_namespace`), no a mano, para no
   desactualizarse en futuros tickets.
3. **Segundo hallazgo, mismo código**: las migraciones de `pack_commerce`/
   `pack_payments` corren DESPUÉS que las del core (`agent-core db
   migrate`), así que la migración del core no alcanzaba las tablas de
   esos packs. Se agregó el mismo fix en `packages/pack-commerce/alembic/
   versions/0005_...` y `packages/pack-payments/alembic/versions/0002_...`,
   confirmado corriendo el runbook completo (`agent-core db migrate`) y
   verificando `pg_class.relforcerowsecurity` antes/después.
4. **Tercer hallazgo, propio del check nuevo**: el grep ingenuo por
   substring "TOKEN" en nombres de campo interpolados en templates
   generaba un falso positivo real contra `costs.html`'s
   `{{ row.tokens_in }}`/`{{ row.tokens_out }}` (conteo de tokens de LLM,
   nada que ver con un secreto). Se corrigió comparando por COMPONENTE
   (split por `.`/`_`) en vez de substring -- `"token" in partes`, no
   `"TOKEN" in nombre`.
5. **`check_app_subscriptions_complete` reusa `list_app_subscriptions`**
   (estado ya persistido por el cron de 15 min), no repite el GET --
   mismo criterio ya establecido en `meta_health.py`.
6. **`check_connections_health_except_billing` reusa `run_health_checks`**
   tal cual (ticket anterior) -- excluye `billing` antes de exigir que
   todo sea `True`.
7. **`check_prod_secret_differs_from_dev`**: nunca compara valores en
   texto plano. Un nuevo setting opcional `DEV_APP_SECRET_HASH` (hash
   SHA-256 del secreto de dev, copiado a mano UNA VEZ a las env vars de
   producción) -- `ok=None` si no está configurado, nunca `True` por
   default.
8. **Heartbeat de worker, genuinamente nuevo**: cron `core.worker_
   heartbeat` (cada 1 min) escribe una clave Redis con TTL 180s -- sin él,
   no había forma de distinguir "la cola existe" de "el worker está vivo".
9. **CLI standalone** (`scripts/verify_production.py`), no un paso del
   `gate.yml`: exit 1 si algún check da `False` (un `None` se imprime como
   advertencia, nunca como fallo duro).

## Hallazgo del auditor-multitenant corregido

Sin hallazgos de severidad alta. Un hallazgo de severidad MEDIA:
`scripts/verify_production.py`'s `--tenant-id` no tiene autenticación ni
autorización propia (a diferencia de las rutas del panel, que siempre
resuelven `tid` desde la sesión autenticada). Evaluación del auditor:
coherente con el diseño del script hoy (nivel de confianza equivalente a
tener acceso al `.env` de producción, es CLI-only, no hay ningún endpoint
HTTP que lo envuelva) -- pero es un vector fácil de reintroducir si
alguien lo envuelve en HTTP más adelante sin agregar autorización.
Corregido documentando explícitamente esa restricción en el docstring del
script (ver sección "SEGURIDAD" en `scripts/verify_production.py`) en vez
de agregar autenticación a un script que hoy no la necesita.

Resto de los focos de auditoría (`check_connections_health_except_billing`
filtra por `tenant_id`; `meta_produccion_page`/`meta_produccion_ejecutar`
derivan `tid` solo de la sesión; `_production_manual_steps` filtra
`MetaSetupChecklist` por `tenant_id`; las tres migraciones `FORCE ROW
LEVEL SECURITY` solo tocan el flag `FORCE`, nunca crean/alteran/borran una
`POLICY`) confirmados sin hallazgo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Enumerar las tablas con RLS a mano en la migración | Ya hay 3 migraciones distintas con listas propias que se desactualizan; una consulta dinámica a `pg_class` es auto-mantenible. |
| Agregar el script de verificación como paso del `gate.yml` de CI | La mayoría de los checks necesitan un despliegue real (URL pública, worker real, tokens reales) -- el `gate` corre contra infraestructura efímera y nunca contra Meta real. |
| Introducir pgTap como dependencia nueva | Ya existe cobertura equivalente en pytest; agregar un segundo sistema de tests solo para este ticket es redundante. |

## Consecuencias

**Positivas:** el checklist es honesto -- corrido fuera de producción real,
la mayoría de los checks dan rojo/`?` en vez de un falso verde; los tres
hallazgos reales (FORCE ROW LEVEL SECURITY faltante en 3 lugares distintos,
falso positivo del grep de secretos) quedan corregidos, no solo señalados.

**Negativas / lo que aceptamos pagar:** `scripts/verify_production.py` no
tiene autenticación propia -- se asume el mismo nivel de confianza que
tener acceso al `.env` de producción (correrlo requiere ya tener esas
credenciales cargadas). No es un endpoint HTTP, es un script de operador.

**Qué haría que revisáramos esto:** si `scripts/verify_production.py`
alguna vez se envuelve en un endpoint HTTP o un webhook de CI/CD
accesible remotamente, hay que agregarle autenticación explícita --
hoy depende enteramente de que solo alguien con acceso al entorno de
producción pueda ejecutarlo.

## Impacto en el código

- `core/gateway/meta_production_readiness.py` (nuevo).
- `core/gateway/meta_preflight.py` (`PreflightCheck` extendido,
  `MANUAL_STEPS` con `advanced_access`/`payment_method`).
- `core/gateway/meta_usage.py`, `core/tasks/celery_app.py` (heartbeat).
- `core/admin/views.py`, `core/admin/templates/meta_produccion.html`.
- `core/config.py` (`dev_app_secret_hash`).
- `scripts/verify_production.py`.
- `alembic/versions/0029_force_row_level_security.py`,
  `packages/pack-commerce/alembic/versions/0005_...`,
  `packages/pack-payments/alembic/versions/0002_...`.
