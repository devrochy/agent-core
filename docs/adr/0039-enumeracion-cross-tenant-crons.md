# ADR-0039 — Enumeración cross-tenant de assets de Meta para crons (SECURITY DEFINER)

- **Estado:** aceptado
- **Fecha:** 2026-09-22

## Contexto

Los crons de core que recorren assets de Meta de **todos** los tenants
(`meta_subscriptions_health`, `message_template_sync`, `meta_pricing_sync`,
`meta_health_check`) enumeraban `MetaConnection`/`WabaAccount` con un `select`
plano, **sin `tenant_scope`**. Como ambas tablas tienen RLS fail-closed
(migración 0014/0015 + `FORCE ROW LEVEL SECURITY` en 0029), esas queries
devolvían **0 filas**: el cron corría como no-op silencioso. Antes morían con
`RuntimeError: Event loop is closed` (bug del puente async del worker, ADR
aparte), así que el no-op quedaba enmascarado por el crash.

Ya existía el patrón correcto para enumerar tenants (`select id from tenants
where ...`, tabla de sistema sin RLS, + `tenant_scope` por tenant) en
`pack_commerce/jobs.py::_tenant_ids` y `core/orchestrator/debounce.py`. Pero
para los crons de Meta eso implica O(tenants) queries por corrida (en un
despliegue con muchos tenants, costoso) y no resuelve limpiamente conteos
globales.

## Decisión

1. **Funciones `SECURITY DEFINER` que devuelven SOLO `id` + `tenant_id`**
   (migración `0033`): `admin_meta_connection_ids(p_statuses text[])` y
   `admin_connected_waba_ids()`. Mismo mecanismo que
   `resolve_tenant_for_phone`/`admin_tenants` (ADR-0012): el dueño omite RLS
   para el bootstrap. Nunca devuelven secretos ni contenido.
2. **La hidratación/operación de cada asset sigue bajo RLS**, con
   `tenant_scope(tenant_id)` y `session.get(...)`. La función solo enumera ids;
   el acceso a los datos sigue protegido.
3. **`REVOKE ALL ... FROM PUBLIC` + `GRANT EXECUTE ... TO app_runtime`**: el
   catálogo cross-tenant no queda ejecutable por cualquier rol.
4. **`webhook_events` NO entra acá**: es tabla de sistema sin RLS, así que
   `count_stalled_webhook_events` ya funcionaba sin scope.

## Por qué es un patrón nuevo (y no una violación de ADR-0012)

ADR-0012 advierte que la resolución `SECURITY DEFINER` **crezca más allá de
"resolver un id"**. Estas funciones enumeran un **catálogo de ids**, no
resuelven un único tenant para una request entrante. La decisión se registra
acá explícitamente para que la excepción sea auditable: la garantía de
aislamiento sigue siendo RLS en la hidratación, y las funciones devuelven
metadata de baja sensibilidad (id + tenant_id).

## Consecuencias

- El rol de migraciones debe ser superuser o `BYPASSRLS` para que el
  `SECURITY DEFINER` omita RLS (con `FORCE RLS` el dueño no-superuser queda
  sujeto a la policy). En el despliegue local `agent_core` es superuser;
  conviene verificarlo en el checklist de producción.
- Alternativa considerada y descartada: enumerar `tenants` + `tenant_scope`
  por tenant (consistente con pack-commerce, pero O(tenants) por corrida).
