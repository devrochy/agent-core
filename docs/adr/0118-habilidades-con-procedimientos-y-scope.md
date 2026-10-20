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

## Adenda (2026-10-11, HU-AOS-14): quién ejecuta el sync de `pack_versions`

`register_pack_version()` (migración 0079) no da `EXECUTE` a `app_runtime`, a diferencia de `upsert_plan()`
(ADR-0045): la ejecuta el rol dueño (`MIGRATIONS_DATABASE_URL`, que ya solo tiene el contenedor `api`) desde
el entrypoint, después de migrar (`agent-core db sync-packs`). Detalle en la adenda de HU-AOS-14 de ADR-0117.

## Adenda (2026-10-17, HU-AOS-16 parte 1): habilidades por conexión y procedimientos por agente

Primera parte de la HU. No cambia esquema (sin migración) ni contratos, y con las banderas apagadas
(`aos_runtime = legacy` o `AOS_CONNECTION_ROUTING` apagada) no cambia lo que ve el modelo: el turno
solo lee lo nuevo cuando tiene una release en la ranura.

- **Scope de negocio, sin cambio:** sigue siendo `enabled_business_packs(spec activo)` (ADR-0019). No
  se escriben filas `pack_installations` de scope `tenant`, ni hay siembra, backfill o doble escritura
  desde `spec.packs`: el punto de corte de §2 (lectores sobre instalaciones, todos los call-sites y el
  CI contra `spec.packs`) es la parte 2, en un solo PR.
- **Scope de conexión:** filas `scope = 'connection'` que escribe solo la API nueva
  (`/api/v1/admin/skills/installations`, sección `plugins`; escritura del master o de un negocio
  `v3`) y que lee solo el build de la release. Si la conexión tiene instalaciones propias vivas y
  activas, la release congela SOLO esas (reemplazo entero, sin merge); si no, las del negocio.
  Precondición: el pack está habilitado en el negocio (409 `pack_not_enabled`), porque la activación
  del código, la config y las tools siguen colgando de `spec.packs`; el build lo vuelve a exigir
  (`ErrorDelBuild("instalacion_sin_habilitar")`, fail-closed). Integración no conversacional → 409.
  La instalación no lleva versión: la fija la release. Instalar o quitar aplica con la próxima release.
- **`release/2`:** cada componente lleva `is_default` y `procedures` resueltos a versión exacta
  (`[{id, version}]`; `0.0.0` = abierto). Los rangos (`^`, `~`, exacta) se resuelven en el build
  contra los `procedure/1` del manifiesto de la versión congelada de cada habilidad (los mismos bytes
  que `pack_versions`, ya comparado el hash) y su `requires` tiene que estar en el scope; si no,
  `procedimiento_sin_resolver`. Los dos campos entran al hash: el primer build tras el deploy crea una
  release nueva por conexión aunque nada más cambie. Una release `release/1` se lee con la regla
  transitoria de HU-AOS-05 (procedimientos del binario de los packs habilitados), decidida por
  `schema`, no por los campos presentes.
- **El General declara, no hereda:** el lector tolerante escribe en `procedures` del General los
  procedimientos publicados de los packs habilitados con `^M.0` (como ya declara en `tools` todo lo que
  ofrece el loop). No hay regla por key: una sola regla de oferta para cualquier agente. Con `v3`, el
  build refresca solo el General y solo si su última versión la produjo el lector tolerante
  (`legacy_agent_spec_id` no nulo); una versión publicada por el dueño no se pisa.
- **Runtime:** `cargar_release` lee además los `procedure/1` de `pack_versions` (caché global en
  proceso por `(pack_id, version)`: son inmutables) y el `content` de las versiones de los agentes.
  La ranura (`ArtefactosDeRelease`, sigue hoja) lleva las habilidades, los procedimientos de toda la
  release y, por `instance_id`, los de cada agente. Los agentes son los `AgenteCongelado` de HU-AOS-15
  (contenido por `agent_version_id`), que con `release/2` traen además `is_default` y sus
  procedimientos resueltos; el agente en foco es el `foco` que elige `elegir_foco` (HU-AOS-15) y
  `procedimientos_del_agente()` deriva de él (un solo modelo, sin enganche propio). Con el motor activo, `iniciar_procedimiento` ofrece solo los procedimientos
  PUBLICADOS que declara el agente en foco (nunca los abiertos `0.0.0`, que solo sirven a la
  revalidación); las tools pasan si son del core, comandos, de la etapa en foco o declaradas por el
  agente, y nunca las de un pack fuera de las habilidades de la release (`fuera_del_agente`). Las
  puertas se calculan sobre todos los procedimientos publicados de la release. Las tools
  declarativas no las filtra el scope, solo el agente. Con el General por defecto, lo que ve el
  modelo es lo mismo que sin release (test de loop guionado).
- **Cambio de release:** `revalidar_tareas` sigue una tarea si la release nueva tiene el mismo
  procedimiento con el mismo major (`procedimiento_para`); si desaparece o cambia de major, se cancela
  o se escala como antes.
- **Límites conocidos de la parte 1:** una conexión con instalaciones propias no puede tener al General
  del lector tolerante (declara todo lo del negocio: el build falla con `procedimiento_sin_resolver`);
  se usa con especialistas. Fuera del turno (worker y crons) `registro.definicion` solo ve el binario,
  así que una release que congele una versión distinta a la del binario no gobierna esos cierres
  hasta la parte 2 (hoy coinciden). El flujo y los playbooks del pack siguen del binario.
- **Parte 2:** lectores `enabled_packs` sobre instalaciones en todos los call-sites y CI contra
  `spec.packs`; instalación de scope negocio; `load_config` por conexión; crons y eventos por
  instalación; `plugin_configs` → `config_encrypted`; tope del plan por instalaciones; plantillas de
  agente; candidatas `origin = platform` y `yanked`; flujo y playbooks desde `pack_versions`; panel.


## Adenda (2026-10-18, HU-AOS-46): plantillas de agente en el core por ahora

Enmienda §1: las plantillas de agente no entran todavía en `pack_versions` (`agent_templates` sigue vacío y ningún
hash de pack cambia). Viven como configuración del core en `core/presets/plantillas/` y solo se usan al crear el
agente; moverlas a los packs queda para la parte 2 de HU-AOS-16. Detalle en ADR-0139.

## Adenda (2026-10-20, HU-AOS-49 parte 3): `ToolContext` con la identidad del agente

**Estado:** aceptada con ADR-0145 §5, que el dueño aprobó el 2026-10-19. Esta adenda reemplaza el
«`ToolContext` no cambia» de §3 en un solo punto: `ToolContext` suma campos.

- `ToolContext` suma `agent_id`, `agent_version_id` y `knowledge`:
  - `agent_id` es `agents.id`, no la instancia;
  - `knowledge` es el bloque congelado de `agentspec/3`.
  - Los tres tienen default `None`, así que el runtime legacy y los tests que arman `ToolContext` no
    cambian.
  - El loop los llena desde `release_vigente.agente_del_turno()`, igual que `write_namespaces`. El LLM
    nunca los elige.
- Es identidad genérica, no forma de un pack. Cualquier pack puede usarla para acotar lo suyo al
  agente que responde.
- `MCPTool` suma `resumen_de_auditoria`: lo que `audit_log` guarda del resultado. Por defecto es el
  resultado completo. Si el resumen falla, se guarda `{"resumen_fallido": true}` (fail-closed).
- **Sin motor de procedimientos no hay filtro por agente:** `oferta.py` solo actúa con release/2 y
  motor. Una tool que tiene que ser fail-closed por agente lo comprueba en su
  `connection_predicate`, leyendo `agente_del_turno().tools`. Es lo que hace `buscar_conocimiento`.
