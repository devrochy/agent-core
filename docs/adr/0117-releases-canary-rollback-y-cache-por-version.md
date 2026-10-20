# ADR-0117 — Releases por conexión, canary sticky por episodio, rollback en un clic y caché por versión

- **Estado:** aceptado (2026-10-09, HU-AOS-01)
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-14, HU-AOS-32 y HU-AOS-35 (SPEC-26). Generaliza ADR-0009. Enmienda ADR-0051 §1 y §4
  (rollback por *flip* de `is_active`; `eval_runs` gana `release_id`), ADR-0026 y ADR-0109 (overrides y secciones del
  negocio) y ADR-0075 (el playground prueba la release candidata y reemplaza al tenant de staging de ADR-0016).
  De ADR-0016 sigue vigente el gate antes de activar y su adenda de HU-SH-08 para los artefactos del repo del negocio:
  aplicarlos pasa a ser `agent-core release build` más ese gate de journeys. Se reemplazan el *flip* de `is_active` y
  la imposibilidad del porcentaje.

## Contexto

- ADR-0009 pidió releases versionadas con canary al 5 % y rollback en un paso.
- ADR-0016 redefinió el canary como gate de evals, negocio de staging y rollback por *flip* de `is_active` (llevado al panel por ADR-0051 §1), porque `load_active_spec` elige una sola versión por negocio y no hay reparto de tráfico.
- Los overrides de playbooks del negocio (ADR-0026) y sus secciones de flujo (ADR-0109) viven en `TENANTS_ROOT`, fuera de cualquier versión.
- En producción ninguna conversación se cierra: solo el playground la pasa a `closed` (`core/admin/playground.py:115`).
- Con agentes, procedimientos, permisos y políticas por conexión, un rollback por partes deja combinaciones nunca probadas.

## Decisión

Cada conexión publica releases inmutables, con canary sticky por episodio, rollback en un clic y la versión dentro de cada clave de caché.

1. **Versiones inmutables:** `agent_versions`, `a2a_graph_versions` y el contenido de las habilidades (`pack_versions`), con la función genérica `prevent_mutation()` (nueva, ADR-0114) y hash del contenido. `pack_versions` la usa con `yanked` como única columna mutable; `task_events`, `release_events` y `a2a_events` (ADR-0116) la usan sin excepciones. Las políticas (presupuestos y límites) viven en el KernelSpec (ADR-0113), que ya se versiona como agente `core`: no hay tablas de políticas aparte. Solo las conexiones conversacionales tienen release; las integraciones (ADR-0115) no.
2. **Release por conexión:**
   - `connection_releases` guarda el manifiesto y `release_components` las versiones de cada instancia.
   - El build congela también lo que hoy vive en el disco: overrides de playbooks (ADR-0026) y secciones `tenant` del flujo (ADR-0109). El manifiesto guarda su texto y hash, y en runtime se leen de la release, no de `TENANTS_ROOT`.
   - El build congela además los campos de `AppSettings` que entran al prompt (`brand_voice`, `assistant_name`, `business_description`, `language`). Lo operativo (horarios, pausa, debounce, `guardrail_fail_action`, ADR-0051/0092) y la configuración de las instalaciones (ADR-0118) siguen en caliente y no entran a la release. El contenido del core (`core/playbooks/`, `core/flow.json`) va con el binario y lo cubre el CI de la release del motor (ADR-0120 y la adenda de ADR-0016), no el canary por conexión.
   - Una release fija la versión exacta de cada habilidad; los rangos del agente (ADR-0113 §2) se resuelven en el build.
   - El ciclo de vida queda en `release_events`, append-only.
   - La conexión guarda punteros `active_release_id` y `canary_release_id`. Las FKs compuestas impiden apuntar a la release de otra conexión.
   - `conversations` gana `release_id` y `episode_started_at`, con FK compuesta `(tenant_id, connection_id, release_id)`.
   - **Migración:** release 1 por conexión conversacional, construida desde el AgentSpec v2 activo (lector tolerante, ADR-0113) y los artefactos del disco, idempotente y detrás de `AOS_RUNTIME` (ADR-0112). Con la bandera en `legacy`, el runtime ignora `release_id`, lee de `agent_specs` y del disco, y sigue el rollback de ADR-0051 §1.
3. **Actualización de habilidades.** Cuando el sync registra una versión nueva de una habilidad, el core arma para cada conexión con una release activa que la usa una candidata que solo cambia ese componente (`origin = platform`) y la promueve con el mismo gate y canary; el dueño ve el aviso y puede detenerla. Así se conserva lo que daba ADR-0109: los cambios de un pack llegan a todos los negocios sin re-sembrar. Una versión marcada `yanked` por seguridad se reemplaza con promoción directa, sin canary, y queda un `release_event` con el motivo.
4. **Sticky por episodio:**
   - Un episodio es un tramo de conversación sin pausas mayores que `episode.idleMinutes` (KernelSpec; 720 min por defecto, a validar).
   - La conversación conserva su release durante el episodio. Al empezar el siguiente, el Coordinador asigna la release activa o la candidata.
   - El **canary** reparte por `hash(canary_release_id ‖ conversation_id) % 100` (≤ 50 %): estable entre episodios de un mismo canary y sin someter siempre a los mismos clientes a cada canary. El reparto lo hace el Coordinador, sin balanceador: se levanta la objeción de ADR-0016.
   - Un rollback, o un canary detenido, reasigna en su siguiente turno las conversaciones de la release retirada.
   - Al cambiar de release, cada tarea abierta se vuelve a validar contra la versión del procedimiento de la release nueva: conserva los datos válidos, su etapa se recalcula y queda un evento `revalidated` con la versión de origen y la de destino, así la proyección sigue siendo reconstruible (ADR-0114). Si el procedimiento ya no está, la tarea se cancela con el motivo `release_changed`; si ya tuvo un efecto irreversible (por ejemplo, un link de pago emitido), se escala en vez de cancelarse. Las tareas de un episodio anterior sin actividad se cierran con `episode_expired` (ADR-0114).
   - Una release sin episodios en curso, que no está activa ni en canary, se puede retirar.
5. **Gate:** no se promueve sin el pass^k aprobado (ADR-0119).
   - El gate corre en el worker contra una base de evaluación separada (`agentcore_eval`, nunca la del negocio), con los datos sembrados de los escenarios de la habilidad y la release candidata, sin efectos externos (sender y pagos en modo eval).
   - Su consumo va a `usage_ledger` con `kind = eval`, a cargo de la plataforma y con tope mensual por negocio. Si la release no toca procedimientos (solo voz o textos), corre solo los procedimientos cuyos textos cambiaron y los journeys del negocio si existen (adenda de ADR-0016).
   - El resultado se guarda en `eval_runs.release_id` y `eval_results.procedure_id` (columnas aditivas y nulas).
   - **Canary:** si hay muestra mínima y no hay no-inferioridad (completitud, escalamiento, costo y p95), el rollback es automático. Sin la muestra mínima no hay veredicto por métricas: ni promoción ni rollback automáticos. El auto-rollback inmediato queda para señales duras (errores de acción y fallos del verificador por encima del umbral). Si la conexión no junta la muestra en 7 días, el dueño promueve a mano con el gate aprobado.
6. **Rollback en un clic:** se cambia el puntero; todas las conversaciones usan la versión anterior desde su siguiente turno (≤ 5 s tras el clic).
7. **Caché por versión:**
   - Las versiones son inmutables, así que lo compilado de una release (prompt fijo, procedimientos, disparadores) se cachea en proceso con la clave `(tenant_id, release_hash, agent_version_hash)` y nunca se invalida.
   - En los bloques fijos solo entran valores congelados en la release; lo que cambia por turno, cliente o conversación va al bloque dinámico (ADR-0091).
   - No se cachean resultados de acciones hasta medir una que lo justifique. Cuando pase, la clave lleva negocio y versión de la acción, con TTL corto, y los *epochs* de datos llegan con el primer evento de dominio real. Sin warm-up.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `is_active` por agente | Rollback por partes, sin atomicidad entre agentes, permisos y políticas |
| Mantener ADR-0016 sin porcentaje | Una regresión desconocida llega al 100 % |
| Sticky hasta cerrar la conversación | En producción nada cierra una conversación: la release quedaría fija para siempre |
| GitOps de la configuración | No apto para dueños no técnicos |

## Consecuencias

**Positivas:** rollback atómico por conexión, incluidos los textos del negocio y los campos de `AppSettings` que entran al prompt; canary real; las releases viejas se drenan solas; caché sin contenido viejo; cada turno queda atribuido a su release.

**Negativas / lo que aceptamos pagar:**
- Más objetos que gestionar.
- El canary necesita tráfico para decidir: con poco tráfico, la promoción es manual.
- El gate cuesta LLM por release (a cargo de la plataforma, con tope por negocio).
- Un override del negocio deja de aplicarse en caliente: entra con la siguiente release.
- Una tarea a medias puede perder datos que ya no validan tras un cambio de release.

**Qué haría que revisáramos esto:** que el canary no detecte regresiones reales en 10 releases, o que `episode.idleMinutes` deje a los clientes frecuentes demasiado tiempo en una versión vieja.

## Impacto en el código

- `core/releases/` (nuevo): build, validación, promoción, canary, rollback.
- `core/kernel/`: selección de release y episodios.
- `core/agents/` y `core/llm/prompt_builder.py`: playbooks, flujo y campos de `AppSettings` se leen de la release; caché en proceso de lo compilado.
- `evals/` y el worker: gate contra `agentcore_eval`; `eval_runs.release_id` y `eval_results.procedure_id`.
- Sync de packs (`pack_versions`) y armado de candidatas `origin = platform`.
- API `/api/v1/connections/{id}/releases` y `/rollback` (ADR-0077), reflejadas en `docs/openapi.json`.

## Adenda de implementación (2026-10-11, HU-AOS-14): build, release 1, sticky y rollback

Revisada por el agente `arquitecto` antes de escribir código. No cambia contratos ni la conducta con
`aos_runtime = 'legacy'` (el gate de evals en replay pasa sin regrabar).

- **Forma `release/1` del manifiesto** (`core/releases/manifiesto.py`). Congela **entradas**, nunca
  contenido del core: `components` (instancia → versión exacta, con `content_hash`), `skills`
  (`pack_id`, `version`, `manifest_hash` registrados en `pack_versions`), `tenant_plugins` (solo por
  nombre), `playbook_overrides` (los de la allowlist, con texto y hash), `flow_tenant` (secciones crudas
  del `flow.json` del negocio, con hash) y `app_settings` (los cuatro campos del prompt).
  `built_with` (git sha, versión de contratos) queda fuera del hash: un deploy no genera releases. Los
  procedimientos abiertos se **derivan** al cargar la release (flujo del core y de los packs del binario +
  las secciones congeladas), no se congelan.
- **Build** (`core/releases/build.py`): versión exacta de cada instancia viva (exige el Coordinador) y el
  validador único del contrato sobre cada una. Con `legacy` el Coordinador y el General se **refrescan**
  antes desde el AgentSpec v2 activo (backfill B2 + publicar la conversión si cambió): así la release 1 sale
  del v2 activo aunque se haya editado después del backfill. Habilidades: `enabled_business_packs` del v2
  activo (la única lectura de la habilitación hasta HU-AOS-16); el build exige su versión registrada, con el
  mismo hash y no `yanked`. Fail-closed con los textos del negocio: un override ilegible o de más de 8 KiB,
  o un `flow.json` roto, hacen fallar el build. Idempotente por `manifest_hash`; el manifiesto se arma fuera
  del lock de la conexión. No se activa sola.
- **Release 1** (`agent-core db backfill-releases`): por cada conexión conversacional sin release, build +
  promoción (`reason = 'release_1'`). Precondición de `v3` para las conexiones que se den de alta después
  (alta y promoción BYOK, ADR-0127 §3): correr el comando de nuevo. Que esos caminos construyan la release
  solos queda para HU-AOS-15.
- **Promoción y rollback** (`core/releases/promocion.py`): promoción total (canary en HU-AOS-32); el gate es
  la función `gate_aprobado` (hoy aprueba, HU-AOS-21) y queda anotado en el evento. Destino por defecto del
  rollback: la release más reciente con `promoted` que no es la activa ni fue retirada por `rolled_back`
  después de ese `promoted` (dos clics no rebotan); sin destino, 409. Deja `rolled_back` en la que se deja y
  `promoted` (`reason = 'rollback'`) en la de destino.
- **Sticky por episodio** (`core/kernel/release_del_turno.py`, solo con `v3` y `AOS_CONNECTION_ROUTING`):
  una conversación cambia de release al empezar un episodio, o **a mitad de episodio** solo si el último
  evento de **ciclo de vida** de su release (`promoted`, `canary_started`, `rolled_back`, `canary_stopped`,
  `invalid`) es `rolled_back`, `canary_stopped` o `invalid`; los eventos del gate no cuentan. Sin columna
  nueva. El `idleMinutes` es el del KernelSpec de la release vigente de la conversación, y la tarjeta de
  estado usa el mismo (una sola definición de episodio). El carril es `stable` (`carril()` es el enganche
  del canary). `core_decisions.release_id`/`release_lane` se llenan desde la ranura del turno.
- **Revalidación**: `revalidated` (versiones y releases de origen y destino; conserva los datos que declara
  la versión nueva, o todos si no declara ninguno; la etapa es la vigente si existe, si no la primera, hasta
  el motor). Sin el procedimiento: `cancelled` con `release_changed`, o `escalated` si hubo un
  `action_succeeded` de una acción irreversible **o sin efecto conocido** en el catálogo (hoy ninguna lo
  declara: HU-AOS-34). Escalar la conversación a una persona desde el orquestador es precondición para
  encender `aos_procedures`; `fijar_release_del_turno` devuelve los ids escalados.
- **Lo que lee el runtime de la release con `v3`**: overrides de playbooks (`load_playbook`), secciones del
  negocio (`resolve_flow`) y los cuatro campos de `AppSettings` del prompt. **Límites** hasta HU-AOS-15: el
  loop sigue ejecutando el AgentSpec v2 de `agent_specs`, así que el rollback es parcial (sin persona,
  modelo, tools ni guardrails) y ningún cliente real debe pasar a `v3` antes; el aviso de fuera de horario
  (`core/kernel/ingress.py`) y las respuestas de respaldo del consumer siguen leyendo `AppSettings` y el
  disco en caliente; el playground todavía no prueba la candidata (ADR-0075).
- **Caché** (`core/releases/cargar.py`): en proceso por `(tenant_id, release_id)`, con tope por bytes
  (64 MiB), nunca se invalida. La clave por `agent_version_hash` de lo compilado llega con HU-AOS-35.
- **Sync de `pack_versions`** (adelantado de HU-AOS-16): `register_pack_version()` (migración 0079,
  SECURITY DEFINER, `search_path` fijo, `REVOKE ALL FROM PUBLIC`, sin `EXECUTE` para `app_runtime`) recibe
  el JSON canónico como **texto**, calcula su sha256 en la base y rechaza la fila si no coincide; solo
  plugins con `owner_tenant_id` nulo; la misma versión con otro hash es `hash_mismatch` (alerta, no se
  registra). Corre con el rol dueño en el entrypoint del contenedor `api` tras migrar
  (`agent-core db sync-packs`), registra antes los plugins en `plugins` y nunca frena el arranque. El CI
  compara los hashes con `packages/pack-versions.lock.json` (`scripts/pack_versions_lock.py`): cambiar el
  contenido de un pack exige subir su versión.
- **Pendiente para la segunda parte (decisión del dueño):** candidatas `origin = platform` al registrarse una
  versión nueva de una habilidad y la promoción directa por `yanked`. ~~Hasta que el runtime lea el contenido
  del pack desde `pack_versions` (HU-AOS-16) la candidata y la activa correrían el mismo contenido del
  binario.~~ HU-AOS-16 parte 1 (adenda en ADR-0118): la release ya lee desde `pack_versions` los
  procedimientos publicados de la versión congelada; el flujo y los playbooks del pack siguen saliendo
  del binario hasta la parte 2. Si van antes, cada candidata se deriva del manifiesto de la release activa cambiando solo esa
  entrada, no de un build nuevo.
- **Rutas:** viven en `/api/v1/admin/connections/{id}/releases` y `/rollback` por el realm (ADR-0077 §2).

## Adenda de implementación (2026-10-18, HU-AOS-15 parte 1): el loop ejecuta la release

Revisada por el agente `arquitecto` antes de escribir código. Cierra los **Límites hasta HU-AOS-15** de la
adenda de HU-AOS-14. No cambia contratos ni esquema (sin migración 0092). Con `aos_runtime = 'legacy'` (o
sin `AOS_CONNECTION_ROUTING`) nada cambia: el gate en replay pasa sin regrabar.

- **Qué ejecuta un turno v3.** La release vigente de la conversación carga también las versiones congeladas
  de sus componentes (`core/releases/cargar.py`, por el `agent_version_id` de cada `components[]` del
  manifiesto, nunca desde las instancias ni los agentes vivos: una instancia deshabilitada después no
  desaparece de una release inmutable). La ranura del turno (`core/shared/release_vigente.py`) lleva esos
  agentes, el especialista **en foco** y las habilidades del manifiesto.
- **Vista v2 de la release** (`core/agents/spec_de_release.py`, `spec_del_turno`): el único punto de enganche
  del loop. El loop lee hoy la forma v2 en ~25 puntos, así que el turno recibe un objeto del contrato v2
  `AgentSpec` armado desde el `agentspec/3` del especialista en foco y el `kernelspec/1` del Coordinador: la
  inversa exacta de la tabla de ADR-0113 §5 (la voz desde `voice` del KernelSpec; tope y frases, modelo,
  memoria, playbooks, bindings y `maxToolCallsPerTurn` del agente; `budgetUsdPerConversation` del
  KernelSpec; `packs` desde `skills` + `tenant_plugins` del manifiesto; `guardrails` y `declarativeTools`
  vuelven a `extensions`, lo único que v2 transportaba ahí). Es un lector tolerante **inverso y
  transitorio**, decisión tomada y revisable: se retira cuando el loop lea v3 directo (HU-AOS-46); la vista
  nunca se persiste ni vuelve a `agent_specs`; nada nuevo del v3 viaja por `extensions` (lo nuevo, como
  `writeNamespaces`, va por la ranura). `usage_ledger.agent_spec_version` = `<key>@v3:<versión>`.
  Invierte de forma transitoria la dirección de ADR-0113 «Impacto en el código» (loop lee v3).
- **Orden del turno:** `fijar_release_del_turno` y `spec_del_turno` corren antes de `politicas_del_turno`,
  así un turno bloqueado por cuota también lleva `release_id` y la instancia, y el texto de cuota sale de la
  release.
- **Agente en foco y dueño** (`core/releases/cargar.py::elegir_foco`, `release_del_turno.asignar`): (1) el
  dueño vigente (`conversations.active_agent_instance_id`) si sigue siendo especialista de la release y el
  episodio no cambió; (2) el `routing.defaultAgent` del KernelSpec; (3) el único especialista; sin
  especialista o con varios sin uno por defecto, `ErrorDeRelease` (fail-closed: sube por el camino de error
  del turno). Un episodio nuevo **suelta** al dueño (NULL → por defecto), como piden `kernelspec.Episode`
  y ADR-0116. `active_agent_instance_id` significa «la instancia que atiende la conversación» y
  `owner_since` desde cuándo; `asignar` nunca escribe un handoff (dos fases, HU-AOS-19). El Coordinador
  nunca atiende (ADR-0112 §1). El router (`route_source ≠ default`) y el fail-closed de ADR-0118 §3 llegan
  con HU-AOS-17; `is_default` de la instancia y `routing.defaultAgent` siguen siendo dos fuentes hasta
  entonces (hoy manda el KernelSpec). *Actualizado por HU-AOS-16, ver su sección al final.*
- **`writeNamespaces` se aplica:** `ToolContext.write_namespaces` = `memory.writeNamespaces` del agente en
  foco (`new_tool_context`, HU-AOS-36): un agente sin `perfil` no puede guardar datos del cliente. En legacy
  sigue `None` (sin chequeo).
- **Trazabilidad por agente** (plomería de «Mi agente y su data», HU-AOS-47): `usage_ledger.{connection_id,
  release_id, agent_instance_id, agent_version_id}` (modelo mapeado), `core_decisions.active_instance_id`
  (desde `anotar_release`) y `focus_task_id` (`anotar_foco`, la tarea en foco al empezar el turno con el
  motor activo), `turn_outcomes.final_instance_id` (= la instancia que atendió, sin handoff). NULL en legacy.
- **Rollback completo:** al volver a una release anterior, el siguiente turno ejecuta la versión congelada en
  ella: persona, modelo, tools/bindings, guardrails, playbooks y `writeNamespaces` cambian (test de
  integración con dos releases).
- **Migración:** `agent-core db migrar-a-v3 --tenant <slug>|--todos [--dry-run] [--volver] [--construir]
  [--apagar-procedimientos]` (`core/releases/migracion_v3.py`). Fail-closed por negocio: exige
  `AOS_CONNECTION_ROUTING`, los backfills de conexiones y agentes sin trabajo pendiente, release activa en
  toda conexión conversacional, y que esa release carga, tiene especialista en foco, pasa el contrato, el
  General concede `perfil` y está **al día** (el hash del manifiesto que se construiría hoy, sin refrescar
  versiones, es su `manifest_hash` y, en legacy, la
  conversión actual del v2 coincide con las versiones congeladas; `--construir` reconstruye y promueve).
  Enciende solo `aos_runtime`; `--volver` rechaza con `aos_procedures` encendida salvo
  `--apagar-procedimientos` (orden inverso de ADR-0112) y reporta las tareas gobernadas abiertas. Escribe las
  banderas con el rol dueño (`MIGRATIONS_DATABASE_URL`, como `sync-packs`); el setter `SECURITY DEFINER` de
  la nota de la HU llega con el panel del master (hecho: `aos_fijar_banderas`, migración 0097; adenda HU-AOS-15
  de ADR-0112, 2026-10-20).
- **Runbook (precondición de producto):** `AOS_CONNECTION_ROUTING` → por negocio: pass^4 antes
  (`evals/journeys.py --k 4`), `migrar-a-v3 --dry-run`, `migrar-a-v3`, pass^4 después con `--runtime v3`
  (igual o mejor; si no, `--volver`), una semana de observación; `aos_procedures` después, con su propio
  pass^4. El `contract` (`connection_id` y `release_id` obligatorios) llega con 2 releases estables.
  **Antes de desplegar en test:** `migrar-a-v3 --tenant solvershub --dry-run` (ya está en v3 + procedimientos:
  al desplegar, su loop pasa del v2 a la release activa; si está desactualizada, `--construir`).
- **Límites y pendientes (parte 2, con dueño HU-AOS-15):** v3 sin release activa (conexión nueva sin
  `backfill-releases`) sigue en legacy con un log de error (`release_missing`): la release al alta de la
  conexión; referencias a secretos en URL/body de las declarativas (enmienda en ADR-0113); los textos de
  respaldo del consumer/ingress y `load_active_spec` dentro de tools del core siguen leyendo v2; el editor
  legacy (`save_spec_version`, `activate_spec_version`) deja de tener efecto en un negocio v3 hasta
  construir una release; los clamps del lector tolerante (`maxLength`, `shortTermTurns`…) cambian un v2
  fuera de rango; `modelPolicy` del KernelSpec sigue sin leerse (ADR-0132 la toma de código).

## Adenda HU-AOS-16: `is_default` en la elección del foco (2026-10-18)

Al integrar HU-AOS-16 con HU-AOS-15 queda un solo modelo de agentes de la release
(`AgenteCongelado`) y una sola elección del foco (`elegir_foco`). Con `release/2` cada componente
congela `is_default` (el agente por defecto de la conexión), así que el orden pasa a ser:
(1) el dueño vigente; (2) el especialista `is_default` de la release; (3) el `routing.defaultAgent`
del KernelSpec; (4) el único especialista; si no, `ErrorDeRelease`. En una `release/1` no hay
`is_default` y la elección es la de HU-AOS-15. **Decisión tomada, revisable:** la configuración de la
conexión (más específica y explícita del dueño) gana sobre el `defaultAgent` del KernelSpec, que el
lector tolerante fija siempre en `general`; HU-AOS-17 puede unificar las dos fuentes.



## Adenda HU-AOS-17: el router elige el foco (2026-10-18)

Con `tenants.aos_routing` ≠ off, `asignar` calcula además la ruta del router (`core/kernel/router.py`) bajo el
mismo lock: tarea en foco → reglas del KernelSpec → disparadores → `elegir_foco`. En `active` el especialista de
la ruta es el foco del turno y se escribe como dueño (`active_agent_instance_id`, `owner_since`; no es un handoff);
en `shadow` solo se registra en `core_decisions.shadow_route`. `ReleaseCongelada` guarda en su caché el `routing`
validado y el índice de disparadores. Detalle en ADR-0138.
