# ADR-0024 — Salud de plugin por tenant (`auto_disable` acotado)

- **Estado:** aceptado
- **Fecha:** 2026-09-12

## Contexto

Cuando una tool de un plugin lanza una excepción no manejada en runtime, el
core la envuelve en `PluginError` (`core/mcp/host.py::execute_tool`) y
`core/orchestrator/loop.py::_execute_tool_safely` llama
`plugin_manager.auto_disable(plugin_id, reason)`, que llama `deactivate()`:
borra las tools/skills/guardrails del plugin de `mcp_host` (un singleton de
PROCESO) y marca `PluginRecord.status = "error"` — una fila global por
`plugin_id`, sin `tenant_id`.

Esto ya causó daño real dos veces en la misma sesión de trabajo (2026-09-11
y 2026-09-12, documentado en `docs/ROADMAP.md` filas 143-149 y en
`docs/audit/2026-09-11-invalid-id-crash.md`): un `variant_id` inválido que
el LLM le pasó a `generar_cotizacion` para UN tenant apagó `pack_commerce`
para TODOS los tenants del proceso; un rechazo 4xx de Wompi para OTRO
tenant hizo lo mismo con `pack_payments`. Cada vez se corrigió el *vector*
puntual (validación de tipos, `PaymentRequestRejected` para distinguir
rechazos de negocio de fallas reales), pero el *diseño* — cualquier
excepción nueva no prevista, de cualquier pack, vuelve a apagar todo para
todos — quedó sin resolver, marcado explícitamente como deuda que
"amerita un ADR aparte".

`ADR-0019` (registry v1) ya resolvió el problema *adyacente* — qué
packs/tools ve cada tenant, vía `AgentSpec.packs`/`enabled_packs()` — y ya
evaluó y rechazó la opción de convertir `mcp_host` en un registro real por
tenant, razonando que es un singleton sin contexto de tenant usado también
por el eval runner, el panel y los crons (cambio de escala mayor, Sprint
4/5). Ese razonamiento sigue vigente: esta decisión NO lo reabre.

## Decisión

Nueva tabla `plugin_tenant_state` (`tenant_id`, `plugin_id` como PK
compuesta, RLS, `deleted_at` por invariante #8), con la convención **sin
fila activa (`deleted_at is null`) = sano**. Registra la salud de un
plugin PARA UN TENANT, independiente de:

- `mcp_host` — sigue siendo el registro global de qué código existe; no se
  toca. Un plugin roto para el tenant A sigue registrado y disponible para
  el tenant B.
- `PluginRecord.status` — sigue siendo el estado de SISTEMA (¿el módulo
  cargó bien al activarse?), correctamente global: si el código no carga,
  no carga para nadie.

`PluginManager.auto_disable_for_tenant(tenant_id, plugin_id, reason)`
reemplaza, en los callers de runtime (`loop.py::_execute_tool_safely`,
`gateway/webhooks.py::_try_process_media`), la llamada a `auto_disable()`
global: solo escribe la fila de este tenant, no llama `deactivate()`, no
toca `mcp_host` ni `PluginRecord`. El filtro de disponibilidad de tools
(`loop.py`, antes de armar el prompt, y `_tool_denial` para un tool_call
fuera de schema) ahora también excluye los plugins marcados con error para
el tenant actual. Un admin puede reactivar (`reactivate_for_tenant`, marca
`deleted_at` -- no hay DELETE físico, invariante #8) desde `/admin/plugins`
una vez corregida la causa. Si el mismo plugin vuelve a fallar para ese
tenant, se reutiliza la misma fila (mismo PK) y se limpia `deleted_at`.

**Dos no-conflaciones explícitas** (para que no se "simplifiquen" por
error en el futuro):

1. Los **guardrails** de un plugin con salud rota para este tenant SIGUEN
   corriendo — `loop.py` reusa la misma variable `enabled` (consentimiento,
   ADR-0019) para guardrails, y NO la cruza con la salud por tenant. Un
   guardrail ya es fail-open ante su propia excepción
   (`orchestrator/guardrails.py`) y puede seguir siendo necesario para
   validar `tool_results` de turnos anteriores aunque la tool ya no esté
   disponible para llamadas nuevas.
2. El **menú admin** (`plugin_manager.active_admin_menu`) tampoco se
   filtra por esta salud — un admin debe poder seguir viendo/gestionando
   datos ya creados de un pack roto (pedidos, leads) aunque el LLM ya no
   pueda usarlo para llamadas nuevas.

`activate_all_configured()` (arranque del proceso) queda fuera de alcance:
solo carga código en `mcp_host`, nunca mira salud por tenant.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Registry por-tenant completo en `mcp_host` (tools/skills/guardrails por `(tenant_id, plugin_id)`) | Mismo motivo que ADR-0019: `mcp_host` es un singleton sin contexto de tenant usado por eval runner, panel y crons — cambio de arquitectura de Sprint 4/5, no un fix acotado |
| Promover a apagado global tras N tenants distintos fallando con el mismo plugin (circuit breaker) | Señal razonable de una falla de infraestructura real, pero agrega complejidad (ventana de tiempo, umbral, estado compartido) sin un caso real que lo justifique hoy — posible mejora futura si se repite un patrón de falla verdaderamente sistémica |
| Agregar `tenant_id` a `PluginRecord` en vez de una tabla nueva | `PluginRecord` es catálogo de sistema (una fila por plugin, sin RLS) — convertirla en una fila por `(tenant, plugin)` mezclaría dos conceptos (¿cargó el código? vs ¿funciona para este tenant?) en la misma tabla |

## Consecuencias

**Positivas:** una excepción nueva no prevista, de cualquier pack, ya no
puede volver a apagar el asistente de tenants sanos — el radio de daño
queda acotado al tenant que la disparó. `purge_tenant()` (borrado GDPR)
cubre la tabla nueva automáticamente porque descubre por columna
`tenant_id` (no hace falta tocar esa migración). El panel admin distingue
ahora tres estados independientes: consentimiento (¿lo quiero?), sistema
(¿el código carga?) y salud (¿funciona para mí ahora mismo?).

**Negativas / lo que aceptamos pagar:** una carrera teórica entre dos
tool-calls del mismo tenant fallando casi a la vez puede perder una
actualización de `error_message` (la última en escribir gana; no afecta la
corrección, solo el detalle del mensaje mostrado) — aceptado por el mismo
criterio de "poca concurrencia real" que ya usa `_activation_lock`. Un
tenant con un plugin roto sigue sin tools de ese plugin hasta que un admin
reactive manualmente — no hay reintento automático tras N minutos (no hay
caso real todavía que lo justifique).

**Qué haría que revisáramos esto:** que el mismo plugin empiece a fallar
para MUCHOS tenants distintos en poco tiempo por la misma causa — señal de
que es una falla de infraestructura real (no de un tenant puntual) y que
el circuit-breaker descartado arriba empieza a valer la pena.

## Impacto en el código

`core/db/models.py` (`PluginTenantState`), `alembic/versions/0012_plugin_tenant_state.py`,
`core/plugin_manager.py` (`auto_disable_for_tenant`, `disabled_plugins_for_tenant`,
`is_disabled_for_tenant`, `reactivate_for_tenant`), `core/orchestrator/loop.py`
(filtro de skills disponibles, `_tool_denial`, `_execute_tool_safely`),
`core/gateway/webhooks.py` (`_try_process_media`), `core/admin/views.py` +
`core/admin/templates/plugins.html` (columna "Salud" + endpoint
`reintentar`), `docs/PLUGIN-CONTRACT.md`.
