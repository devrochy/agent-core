# ADR-0118 — Habilidades versionadas con procedimientos, consultas y plantillas de agente; instalación con scope

- **Estado:** aceptado (2026-10-09, HU-AOS-01)
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-16 y HU-AOS-34 (SPEC-26). Enmienda ADR-0019 (manifiesto, habilitación por `spec.packs` y
  consentimiento *fail-open* transitorio), ADR-0089 §3 (habilitación del plugin del negocio) y ADR-0110 §3 (habilitar y
  escribir la config de un preset van a `pack_installations`, con scope de negocio y `pending_config` como pendiente,
  hasta el ADR de HU-AOS-46). Usa ADR-0027, ADR-0070 y ADR-0094/0126.

## Contexto

- La habilitación de un pack vive en `AgentSpec.packs` y su configuración en `plugin_configs`, con scope de negocio.
- El consentimiento de acciones es *fail-open* cuando no hay binding (`core/orchestrator/loop.py::_tool_denial`: sin binding no hay denegación). ADR-0019 lo aceptó como transitorio y dejó escrito cuándo apretarlo a fail-closed.
- No hay versión del contenido de un pack (playbooks, flujos, descripciones) que una release pueda fijar.
- Un mismo binario no puede tener dos versiones del código de un pack.

## Decisión

Las habilidades de la plataforma publican versiones inmutables con procedimientos, consultas y plantillas; se instalan con scope y sus acciones son fail-closed por agente.

1. **`pack_versions`** es un catálogo de sistema sin `tenant_id`. Es una excepción a la invariante 1, como `plans` (ADR-0045), `connection_types` (ADR-0069) y `webhook_events` (ADR-0028). Congela el contenido de cada versión:
   - procedimientos (superpoderes);
   - consultas tipadas;
   - acciones con metadatos v2 (versión, efecto, timeout, reintento, respaldo, clases de datos). El respaldo es un código de resultado cuyo texto sale de las respuestas configurables (ADR-0017, invariante 3), nunca texto literal en el manifiesto;
   - plantillas de agente;
   - playbooks con hash y texto;
   - schemas y escenarios de prueba.

   Solo entran las habilidades de la plataforma (`packages/pack-*`). Los plugins del negocio (ADR-0089) quedan fuera, porque el catálogo es global y lo vería cualquier negocio. `app_runtime` solo tiene SELECT. El sync del arranque inserta con una función `SECURITY DEFINER` propia (mismo patrón que `upsert_plan()` de `sync_plans()`, ADR-0045, pero con `ON CONFLICT DO NOTHING`; `upsert_plan` hace `DO UPDATE`). Solo `yanked` puede cambiar: la tabla usa `prevent_mutation('yanked')` (ADR-0114). Si la misma `(pack_id, version)` llega con otro `manifest_hash`, el arranque alerta y no la registra; el CI falla si cambia el contenido de un pack sin subir su versión. La migración marca la tabla como de sistema para el hook de migraciones y `docs/MULTITENANT.md` la lista.
2. **`pack_installations`** con scope de negocio o de conexión:
   - La instalación de conexión reemplaza entera a la del negocio, sin merge (criterio de ADR-0026).
   - `plugin_manager.load_config` resuelve por la conexión del turno (contextvar del Coordinador) y, si no hay, por la del negocio; crons, eventos y panel usan la del negocio. Los crons y eventos de un pack corren si existe alguna instalación activa en el negocio, con cualquier scope. `ToolContext` no cambia (invariante 4).
   - `enabled_packs()`, `enabled_business_packs()` y `count_tenant_packs()` (`core/agents/spec.py`) son las únicas lecturas de la habilitación. Pasan a ser `async`, dejan de recibir el spec y leen de `pack_installations`: con la conexión del turno en contexto devuelven las instalaciones que aplican a esa conexión; sin conexión, las de scope de negocio más las de cualquier conexión del negocio. Sus call-sites cambian en el mismo PR, y fuera del lector tolerante nadie lee `spec.packs`. El tope de habilidades del plan (ADR-0110, `core/billing/metrics.py`) cuenta packs distintos del negocio.
   - **Migración:** desde el backfill (una instalación de negocio activa por cada pack de `enabled_business_packs(spec activo)`, con su fila de `plugin_configs` si existe, cifrado tal cual; las filas de `plugin_configs` de packs no habilitados se migran como instalación inactiva), `pack_installations` es la única fuente, sin importar la bandera. `spec.packs` queda de solo lectura hasta retirar v2; un rollback del AgentSpec (ADR-0051) ya no cambia los packs.
   - La instalación no lleva versión de habilidad: la fija solo la release (una sola fuente, ADR-0113 §4).
   - Un plugin del negocio (ADR-0089) se instala en `pack_installations` igual que una habilidad (`pack_id` → `plugins(id)`); como la instalación no lleva versión, no necesita fila en `pack_versions`. Su contenido es el del binario y la release lo fija solo por nombre de instalación.
3. **Acciones fail-closed por agente:**
   - Un agente usa las acciones de la etapa en foco, las del core, los comandos y las que su versión declara en `tools`. Sin binding no hay acción.
   - Declarar un procedimiento consiente los scopes de sus acciones.
   - El agente General que sale del lector tolerante declara en `tools` las acciones que hoy ofrece el loop: las del flujo v2, más las `always_available` y las declarativas que nombren las secciones `tenant` (ADR-0113), así no pierde ninguna fuera de foco.
   - Con `AOS_RUNTIME=legacy`, `_tool_denial` sigue fail-open (ADR-0019). El fail-closed rige para los agentes v3 y para el General del lector tolerante.
4. **Código = binario único** (invariante 2). Los metadatos de ejecución (efecto, timeout, reintento, scopes) los declara el código del binario; `pack_versions` los copia para el linter y el panel, y el runtime usa los del binario. Subir el riesgo de una acción es un cambio rompedor: una acción con cambio rompedor se publica como `nombre_v2` y convive con la anterior. `nombre_v1` se retira cuando ninguna release activa, en canary o con episodios en curso la use; al arrancar, una release que referencia una acción ausente del binario queda `invalid` y alerta.
5. **Secretos con scope, sin tabla nueva:** viajan cifrados en `pack_installations.config_encrypted` (ADR-0027), con el scope de la instalación. Lleva solo la config de la habilidad (por ejemplo, las llaves de Wompi de pack-payments, ADR-0049) y los secretos de las `declarativeTools` (ADR-0113), estos en una instalación `core` por scope que solo guarda secretos: no habilita nada, `enabled_packs` la ignora y no cuenta para el tope del plan. Los secretos de una integración que es una conexión siguen en la conexión: Google (ADR-0070) y Meta (ADR-0094/0126). Las filas migradas no tienen AAD (ADR-0027 solo la usa en `meta_tokens`); las escrituras nuevas atan el cifrado con AAD a `(tenant_id, installation_id)`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Seguir con `spec.packs` + `plugin_configs` | Sin scope de conexión ni versión de contenido |
| Varias versiones de código de un pack en el proceso | Imposible sin aislar imports; viola el binario único |
| Mantener el consentimiento *fail-open* | Un agente podría usar acciones de otro |
| Tabla `vault.credentials` aparte | Duplica `config_encrypted`, `purge_tenant` no la recorre (solo mira `public`) y no tiene vía de escritura |

## Consecuencias

**Positivas:** las releases fijan el contenido que probaron; cada conexión instala lo suyo; las acciones son fail-closed; los secretos siguen el scope de la instalación.

**Negativas / lo que aceptamos pagar:** sincronizar el catálogo al arrancar; acciones duplicadas por un tiempo (`_v2`); migrar `plugin_configs`; los plugins del negocio no publican procedimientos en v1.

**Qué haría que revisáramos esto:** una necesidad real de versiones de código distintas por negocio (llevaría al plugin del negocio, ADR-0089, o a la topología), o un plugin del negocio que necesite publicar procedimientos (haría falta un dueño y RLS en el catálogo).

## Impacto en el código

- `core/plugin_manager.py`: sincronización de `pack_versions`, instalaciones, `load_config` por conexión y el arranque (`_tenant_with_config`).
- `core/agents/spec.py` (`enabled_packs`, `enabled_business_packs` y `count_tenant_packs` desde `pack_installations`) y **todos** sus call-sites y lecturas de `spec.packs` en `core/`, `cli/` y `packages/pack-*` (entre otros `core/billing/metrics.py`, `core/orchestrator/loop.py`, `core/presets/apply.py`, `core/provisioning/` y `core/admin/settings_service.py`). El CI falla si queda una lectura de `spec.packs` fuera del lector tolerante.
- `core/mcp/` (metadatos v2 de acciones y permisos) y `core/orchestrator/loop.py` (`_tool_denial`).
- `packages/pack-*/pyproject.toml` (manifiesto).
- `docs/MULTITENANT.md` (tabla de sistema nueva) y migraciones.
