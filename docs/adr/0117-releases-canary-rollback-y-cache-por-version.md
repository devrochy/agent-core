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

## Adenda propuesta HU-AOS-21: el gate de pass^k (2026-10-25)

Propuesta. Decisiones del dueño del 2026-10-25 y diseño del gate de §5, ya revisado por el `arquitecto`.
Se entrega en tres partes:
(1) esta adenda;
(2) qué tocó la candidata, la anotación del gate en cada promoción, exenciones que no se pueden
falsificar y los escenarios por procedimiento;
(3) el corredor contra `agentcore_eval`, que escribe veredictos y cierra el gate.

**Decisiones del dueño (2026-10-25):**

- **Costo.** pass^4 con LLM real, solo de los procedimientos que tocó la candidata. Es k = 4, no el k = 8
  que ADR-0119 §1 pedía antes de una release: se enmienda ADR-0119 en ese punto. Lo paga la plataforma,
  con tope mensual en USD por negocio. Al llegar al tope, la promoción queda bloqueada salvo que el master
  la fuerce.
- **Dónde.** Base `agentcore_eval` compartida por entorno, en el mismo Postgres y migrada con el despliegue.
  Cada corrida siembra un negocio efímero con la release candidata y lo borra al terminar.
- **Umbral.** pass^4 ≥ 0,85 por procedimiento tocado **y** no menor que el de la release activa para ese
  procedimiento.
- **Exenciones**, que nunca se deducen del `reason` que manda un cliente:
  - release 1;
  - `migrar-a-v3`;
  - «Preparar mis datos» del Centro de pruebas, solo en negocios de prueba;
  - hotfix por habilidad retirada (`yanked`, §3): pasa directo y se evalúa después sin bloquear;
  - **forzado por el master** con motivo escrito.
- **Sin procedimientos tocados, pasa sin evaluar.**
- **Transición.** Hasta que exista el corredor (parte 3), una candidata que toca procedimientos y no tiene
  veredicto **pasa**, con `gate = 'sin_evaluar'` anotado en el evento. La parte 3 cierra el gate.

**Diseño:**

1. **El gate lee, no mide.** `gate_aprobado` sigue dentro del `FOR UPDATE` de `promover`.
   - **Base del veredicto.** Solo vale el último veredicto de la release cuyo `data.base_release_id` es la
     `active_release_id` leída en ese mismo `FOR UPDATE`. Un veredicto contra otra base cuenta como
     inexistente: después de un rollback u otra promoción, hay que volver a medir. `release_id` ya determina
     `manifest_hash`, porque `connection_releases` es inmutable y única por hash.
   - **Cuándo se mide.** La medición se encola al pedir la promoción (409 `gate_pendiente` y se encola) o
     con «Evaluar» en el panel, **no** en `built`. Así no se gasta tope en borradores del editor ni de los
     presets. Es idempotente por `(release_id, base_release_id)`, con `pg_advisory_xact_lock` por negocio al
     encolar.
2. **Qué tocó la candidata** (`core/releases/diferencias.py`). Se cargan las dos releases con el mismo
   binario (`ReleaseCongelada.procedimientos()` y `procedimientos_de`) y se compara el contenido canónico de
   cada `Procedure`.
   - **Tocado por contenido:** un procedimiento distinto, nuevo o de otro dueño. Uno que desaparece no se
     mide.
   - **Tocan todos los procedimientos de los agentes afectados:**
     - `content_hash` distinto del agente;
     - overrides cuyo nombre está en los `playbooks` del agente (o en la allowlist v2);
     - `app_settings`;
     - `tenant_plugins`;
     - un `skills[].manifest_hash` distinto, para los agentes que usan ese pack.
   - **Coordinador:** un `content_hash` distinto del componente `core` (KernelSpec: voz, reglas, `routing`)
     toca todos los procedimientos de todos los especialistas.
   - **Componentes:** alta o baja de un componente, o un cambio de `is_default`, tocan todos los
     procedimientos del especialista afectado.
   - **Sin activa o con activa `release/1`:** todos los procedimientos de la candidata cuentan como tocados,
     sin línea base.
   - **Aproximación conocida:** un `content_hash` que cambia solo por campos que no entran al prompt (por
     ejemplo, presupuestos) cuenta como tocado. Es más simple y más seguro.
3. **Escenarios.** Cada escenario declara `procedimientos: [id]`. En la parte 2 siguen en
   `evals/journeys/procedimientos/`.
   - **Pendientes** (menos de 2 escenarios): `cambiar_plan` y `promesa_de_pago` (0), `tomar_pedido` y
     `agendar_cita` (1).
   - **El test del CI falla** si un procedimiento fuera de la lista tiene menos de 2, o si uno de la lista
     llega a 2 y sigue listado. Así la lista solo se achica.
   - Esto se aparta del Gherkin, que dice «el build falla»: el build no puede ver escenarios que no están
     en el pack. Moverlos al pack y congelarlos con `pack_versions` va en la parte 3.
   - Escribir escenarios que nunca corrieron no demuestra nada, y grabarlos es LLM real con OK del dueño.
4. **Exenciones sin falsificar.**
   - **Release 1:** la conexión no tiene `active_release_id`, leída en el `FOR UPDATE`; no se mira el motivo.
   - **`migrar-a-v3`, Centro de pruebas y hotfix `yanked`:** pasan un parámetro de palabra clave
     `exencion=...` de `promover`, que la API nunca envía. El del Centro de pruebas además exige que el
     negocio sea de prueba.
   - **Motivos reservados:** `migrar_a_v3` y `qa_datos` se suman a `_MOTIVOS_RESERVADOS` de la API.
   - **Forzado (parte 3):** va en el body del promote con `forzar: true` y `motivo` (≤ 200 caracteres,
     tratado como dato), solo con `actua_como_master`. Queda en el evento y en `audit_log`.
5. **Pass^4 de un procedimiento** = fracción de sus escenarios con `pass_k = 1` (modelo de la migración
   0058). Con 2 o 3 escenarios, ≥ 0,85 equivale a 100 %.
   - **«La activa»** = el valor de ese procedimiento en el último veredicto `approved` de la release activa,
     medido con los mismos `escenarios_sha256`. Si no lo hay (la activa fue `sin_evaluar`, `sin_cambios`,
     exenta o forzada), solo aplica el umbral.
6. **Migración (parte 3)**, aditiva:
   - `eval_runs.release_id`, con FK compuesta `(tenant_id, release_id)` →
     `connection_releases(tenant_id, id)`;
   - `eval_runs.origen` (`gate`, `manual`, `nightly`);
   - `eval_results.procedure_id`, sin CHECK, como dejó la migración 0070;
   - índices `(tenant_id, release_id, started_at DESC)` y `(tenant_id, run_id, procedure_id)`.

   El veredicto (`approved`/`rejected`) es un `release_events` con `data` =
   `{run_id, intento, k, umbral, base_release_id, git_sha, tocados: {id: motivo},
   por_procedimiento: {id: {pass_k, activa, ok, escenarios_sha256}}, motivo}`. No hay tabla nueva.
7. **Qué se escribe en cada base (parte 3).**
   - **Durante la medición,** solo en `agentcore_eval`, con el negocio efímero. El corredor es su propio
     proceso o cola, con un engine hacia `agentcore_eval` y otro hacia la base del negocio. Hay que copiar
     `agent_versions` y `pack_versions` a la base de eval.
   - **Al terminar,** en UNA transacción dentro del `tenant_scope` del negocio real:
     - `eval_runs` (`origen = 'gate'`, `release_id`) y sus `eval_results`;
     - una fila agregada de `usage_ledger` con `kind = 'eval'`, más `connection_id` y `release_id` de la
       candidata (`ck_usage_release_conn`);
     - el `release_event` del veredicto.
   - **`agentcore_eval` no es una base de negocio:** no guarda datos de clientes reales. Por eso borrar el
     negocio efímero no es `tenant purge` ni pasa por `purge_tenant`. Va también en `docs/MULTITENANT.md`
     como excepción declarada al invariante 8.
8. **Tope y cuotas (parte 3).**
   - El tope se guarda en `tenants.settings.evals.tope_usd_mensual`, que escribe solo el master. Por
     defecto vale `EVAL_TOPE_USD_MENSUAL`.
   - El consumo se lee de `usage_ledger` con `kind = 'eval'` del mes.
   - **Toda lectura de gasto del negocio excluye `kind = 'eval'`**: cuota de costo
     (`core/billing/metrics.py`), costos, tablero, data por agente y consola de conexiones. Un test lo fija.
9. **Golden de ruteo (Gherkin «Por ruta»).** Va en la parte 3. Cuando cambien el KernelSpec, los ejemplos
   o los disparadores, corren los disparadores contra el golden aplicado a la release y se reporta la
   precisión por procedimiento. Hoy es un test de pytest con catálogos fijos (HU-AOS-17).

**Lo que no cambia:** el rollback no pasa por el gate; el canary y la no-inferioridad son de HU-AOS-32.

### Parte 3b realizada: el corredor del gate (2026-10-25)

El corredor ya existe y escribe veredictos; **el gate todavía no se cierra** (una candidata sin veredicto
sigue pasando con `gate = 'sin_evaluar'`). Qué quedó y qué se decidió al construirlo:

- **`agentcore_eval` es una base de datos, no un comando.** Mismo Postgres, creada a mano una vez por el
  dueño de la infraestructura (`CREATE DATABASE agentcore_eval`) y migrada con
  `agent-core db migrate --eval` (el `entrypoint.sh` lo corre sin abortar si falla, cuando
  `EVAL_MIGRATIONS_DATABASE_URL` está definida). Variables: `EVAL_DATABASE_URL`,
  `EVAL_MIGRATIONS_DATABASE_URL`, `EVAL_REDIS_DB` (índice lógico distinto del del negocio, 15 por defecto)
  y `EVAL_TIMEOUT_SEGUNDOS` (3600).
- **Entrada manual:** `agent-core db evaluar-release` mide una release de un negocio. Con el LLM real exige
  `--confirmo-costo`; con `--grabacion replay` corre desde `evals/cassettes/` sin red. No hay todavía tarea de
  Celery ni endpoint: el encolado y el 409 `gate_pendiente` son el seguimiento.
- **Subproceso.** `core/releases/corredor.py` reclama, lanza y cierra; la medición corre en
  `evals/gate_release.py` como subproceso con entorno armado desde cero (lista blanca: sin secretos de
  canales, cobro, correo ni push; `DATABASE_URL` y `REDIS_URL` son los de eval; `APP_SECRET_KEY` y
  `TOKEN_MASTER_KEYS` son claves desechables por corrida, nunca las de la plataforma; corre con `cwd` fuera
  del repo para no recargar un archivo de entorno; `EVAL_REDIS_DB` no puede ser el índice del negocio). Razón: los journeys parchean
  el proceso (pasarela y correos simulados) y `core/db/session.py` tiene un engine global; no se pueden
  convivir con el servidor. El subproceso se niega a correr si la base no se llama `agentcore_eval[_x]`, si
  es la del negocio o si tiene negocios que no son suyos.
- **Foto de la release (`core/releases/snapshot.py`).** Se exporta lo que la release congeló (componentes,
  versiones, agentes, instancias y `pack_versions`) y se importa bajo un negocio efímero conservando los UUID.
  Nunca viaja un dato del cliente. Como los UUID se conservan, las mediciones se serializan con un candado
  de sesión en la base de eval (**concurrencia de worker = 1**).
- **Borrado del efímero.** El negocio `gate-eval-*` se suspende y se borra con `purge_tenant` (SQL) y
  `purge_tenant_redis`, sin frase ni master: el id lo calcula el proceso y la base es la de eval. Es la misma
  excepción de la invariante 8 que `tenant purge`, ampliada a este caso (ver `docs/MULTITENANT.md`). Al
  arrancar, una corrida limpia los restos de otra caída.
- **Escenarios** salen de `evals/journeys/` de la imagen (el `Dockerfile` ahora copia `evals/`, sin las
  carpetas pesadas). Con `aos_routing = off` en el efímero, un procedimiento que no es del especialista foco
  es `no_medible`; uno sin escenarios es `sin_escenarios`: ninguno de los dos produce un veredicto `approved`.
- **Presupuesto y gasto.** El presupuesto de la corrida es lo que queda del tope mensual del negocio e incluye el
  costo del cliente simulado; al pasarse, la corrida se corta. El gasto (también el parcial de una corrida
  fallida) queda en `usage_ledger` con `kind = 'eval'`, pagado por la plataforma (créditos 0).
- **Infra nunca rechaza.** Una falla del subproceso (tiempo, grabación faltante, salida inválida, base de eval
  ausente) deja un evento `evaluated` con `estado = fallida` y gasto parcial, nunca `rejected`. Cinco
  fallidas en 24 horas por `(release, base)` cortan los reintentos. Un reclamo `en_curso` vence a los
  `EVAL_TIMEOUT_SEGUNDOS`. La salida del subproceso se valida contra lo pedido (es dato, no instrucción).
- **Pendiente para cerrar el gate** (seguimientos): tarea de Celery y worker con concurrencia 1; endpoint
  «Evaluar» y 409 `gate_pendiente` con el flag por negocio; mover los escenarios a los packs; los escenarios
  PENDIENTES de la parte 2; grabar con LLM real (necesita OK del dueño y medir el costo de la primera
  corrida); tope global de la plataforma; panel.

### Parte 4 propuesta: cerrar el gate (2026-10-25)

Propuesta revisada por el arquitecto. El dueño aprobó el plan el 2026-10-25 (cerrar el gate, con el PR del panel aparte) y decidió que un cambio sin escenarios medibles **pasa anotado como «sin medir»**.

1. **Bandera por negocio, apagada por defecto:** `tenants.settings.evals.gate = "cerrado"` (la ausencia es abierta), junto al tope.
   - Es JSON y no una columna con guarda: solo se lee al promover, no en cada turno. El precedente es ADR-0148 y ADR-0125.
   - La escribe solo el master con `GET/PUT /api/v1/admin/platform/tenants/{id}/gate`, usando `{cerrado, tope_usd_mensual}`.
   - Se escribe con un UPDATE atómico que mezcla dentro de `settings->'evals'` (`core/releases/gate.py`), nunca reescribiendo el dict.
   - El cambio queda auditado como `release_gate_bandera`.
   - El tope va acotado (finito, entre 0 y 100 USD).
   - Encender con `cerrado: true` sin `EVAL_DATABASE_URL` y `EVAL_MIGRATIONS_DATABASE_URL` da 409 `sin_base_de_eval`.
   - Se corrige `core/provisioning/consumer.py`, el único escritor de `settings` sin `FOR UPDATE`, que podía perder la clave y abrir el gate en silencio.
2. **`gate_aprobado` con la bandera, dentro del `FOR UPDATE`:**
   - Si la candidata toca procedimientos y no hay veredicto contra la activa:
     - si la última medición contra esa base quedó `sin_escenarios`, pasa con `gate = 'sin_medir'`;
     - si no, `gate = 'pendiente'` y `promover` responde **409 `gate_pendiente`**, con `details` = `{estado, base_release_id}` de la última medición.
   - Con la bandera apagada, todo sigue como hoy (`sin_evaluar`).
   - Las exenciones, `sin_cambios`, `rejected` (409 `gate_rechazado`) y el forzado del master no cambian.
   - Un veredicto `approved` que dejó procedimientos sin medir (`sin_escenarios` o `no_medible`) los guarda en `sin_medir` del evento, y la promoción los anota.
3. **Encolado** (`gate.solicitar_medicion`, un solo servicio para el 409 y para «Evaluar»):
   - Va en el `except` del router: el `raise` dentro de la sesión ya hizo rollback y soltó el `FOR UPDATE`.
   - Lee la última medición con la misma consulta del corredor, factorizada.
   - No encola si hay una `en_curso` vigente o si está en `demasiados_intentos`. `tope_alcanzado` y `sin_escenarios` se vuelven a pedir: el corredor los recalcula sin gastar LLM (mes nuevo, tope subido, escenarios nuevos).
   - Si falla el encolado, queda en el log y se responde igual el 409 con `estado = no_encolada`.
   - El encolado es idempotente: hay una sola tarea pendiente por `(negocio, candidata, base)` (clave de Redis `gate:pendiente:…` con `SET NX`, que suelta la tarea al terminar). Repetir clics no llena el worker.
4. **Tarea `core.releases.evaluar_candidata`**, en la cola y el worker de siempre (no hay servicio nuevo en Coolify):
   - `time_limit` y `soft_time_limit` por encima de `EVAL_TIMEOUT_SEGUNDOS`, y `acks_late=False`. Así Celery no mata la medición a los 300 s ni deja huérfano el subproceso, y Redis no la vuelve a entregar cuando vence el reclamo.
   - La «concurrencia 1» la da un **candado de Redis** (`gate:corredor`, `NX`, que vence con el límite duro de la tarea y se suelta con «comparar y borrar») tomado antes de `corredor.medir`. Si está ocupado, la tarea se reprograma con `countdown` sin escribir nada. Al agotar la espera (2 h), deja `fallida/corredor_ocupado` para que el panel lo muestre y se pueda volver a pedir.
   - Sin las variables `EVAL_*` en el worker, deja `evaluated` con `fallida/sin_base_de_eval` sin reclamo ni foto.
   - Comprueba que el negocio exista y no esté suspendido, y el módulo entra en el `include` de `celery_app`.
   - Un worker dedicado (`-Q gate -c 1`) queda para cuando haya volumen y el dueño lo apruebe.
5. **API de la conexión:**
   - `POST .../releases/{rid}/evaluate`, con propietario o master, el candado legacy y la bandera encendida. En la misma petición calcula los tocados y responde 200 sin encolar si es `exento`, `sin_cambios` o `ya_medida`; 409 `ya_activa` si es la activa. Si no, encola y responde 202.
   - `GET .../evaluation` (sección `connections`): la medición contra la activa de ahora, con estado, costo, veredicto por procedimiento y lo `sin_medir`, sin textos de escenarios.
6. **Fuera de esta parte:**
   - crear `agentcore_eval` y las variables `EVAL_*` en test, y grabar con LLM real (las dos cosas necesitan el OK del dueño). Con la bandera encendida, cada promoción mide con LLM real;
   - los escenarios pendientes de la parte 2, pasar los escenarios a los packs y el golden de ruteo;
   - el tope global de la plataforma;
   - el panel.

## Adenda propuesta HU-AOS-27: versiones en el panel (2026-10-25)

Propuesta, revisada por el arquitecto. Decisiones del dueño del 2026-10-25:
- de las candidatas de la plataforma entran el aviso (texto del panel) y «Detener»; generarlas desde una versión nueva de una habilidad (§3, parte 2) va en otra HU;
- con el gate apagado se publica con aviso («sin pruebas automáticas: pruébala en el chat de prueba»); con el gate cerrado y sin veredicto aprobado, la acción no está disponible y el panel ofrece «Medir ahora».

1. **Diferencias legibles** (`core/releases/diff_legible.py`, puro, con los nombres legibles que arma el router). Compara la candidata con otra release de la **misma conexión** (por defecto la activa; si no, 404), cargadas con el mismo binario:
   - **agentes:**
     - alta, baja, cambio de versión y cambio de agente por defecto;
     - las **claves de primer nivel** del contenido que cambiaron (el panel las traduce; las que no reconoce van a «otros cambios», así un `content_hash` distinto nunca queda sin explicación);
     - herramientas y procedimientos añadidos y quitados por nombre. De `declarativeTools` solo sale el `name`, nunca `url`, `headers`, `body` ni `parameters`;
   - **superpoderes:** habilidades con su nombre (`plugins.name`) y plugins del negocio;
   - **permisos:** enlaces A2A añadidos, quitados o con otros modos;
   - **otros:** los nombres de los campos de `app_settings` que cambiaron, overrides de playbooks y flujo;
   - **qué se medirá:** `procedimientos_tocados`, con el título de cada procedimiento. Es el mismo comparador del gate: el diff no puede decir «sin cambios» si el gate pide medir.

   Contra una activa `release/1` (sin procedimientos por agente ni `is_default` congelados), el diff lo dice (`sin_linea_base`) y no inventa cambios de agente por defecto.

   `GET /api/v1/admin/connections/{id}/releases/{rid}/diff?against={rid2}`, con la sección `connections` en lectura. Cargar releases viejas para comparar ocupa la caché de releases (64 MiB): se acepta, cuesta recargas y no fallos.
2. **Los permisos entran en el gate.** `procedimientos_tocados` compara `a2a_graph`: si el `content_hash` es igual no hay cambio; si no, compara enlace por enlace por `(from, to)`, incluidos modos, intenciones y límites. Un enlace añadido, quitado o cambiado toca los procedimientos de los agentes `from` y `to` que están en la conexión (motivo `permiso:<key>><key>`); los demás enlaces son inertes. Esto supera la nota de la adenda HU-AOS-18 de ADR-0116 (§2, «el gate no mira `a2a_graph`»), porque `encargar`, `consultar` y el handoff ya existen (HU-AOS-19).

   **Cobertura del veredicto:** un `approved` solo vale si cubrió todos los procedimientos tocados de ahora (los medidos más los `sin_medir`). Si el comparador encuentra más (por un cambio del binario, como este), cuenta como sin veredicto y se vuelve a medir.
3. **Detener una candidata** (`POST .../releases/{rid}/stop`). Exige lo mismo que promover: la sección en escritura, propietario o master y el candado legacy.
   - Bajo el `FOR UPDATE` de la conexión registra `invalid` con `data.reason = "detenida_por_dueno"`.
   - Es `invalid` y no `retired`: `retired` queda para el drenado del §4 (limpieza), y la parada del dueño se distingue por `data.reason`. `_exigir_utilizable` ya impide promoverla.
   - Errores 409: `ya_activa` (para eso está volver atrás), `ya_detenida` y `es_canary`. Detener la canary es `canary_stopped` con el puntero a NULL, y llega con HU-AOS-32.
   - **Es definitivo para ese contenido:** como el build es idempotente por `manifest_hash`, volver a construir lo mismo devuelve la release detenida. La API responde 409 `version_detenida` («esa combinación se detuvo: cambia algo para crear una versión nueva»), en lugar de dejar una candidata que nunca se podrá publicar.
4. **Listado y detalle:** `ReleaseSummary` suma `lifecycle`, el último evento de ciclo de vida, porque `last_event` lo pisan los eventos del gate. Una release con una habilidad `yanked` no se refleja ahí: el panel muestra el 409 `habilidad_retirada` al intentarlo.

## Adenda propuesta HU-AOS-29: métricas de fiabilidad por conexión y versión (2026-10-25)

Propuesta revisada por el arquitecto. Decisión del dueño del 2026-10-25: la consistencia sale de las mediciones del gate, porque todavía no hay nightly. Un nightly real por negocio (con LLM, `origen = 'nightly'`) queda como HU aparte.

1. **Un solo servicio de métricas** (`core/metricas/fiabilidad.py`), de solo lectura, con un `Filtro` (negocio, ventana, conexión y, opcionalmente, versión y carril) que se aplica a **todas** las métricas. El canary de HU-AOS-32 las compara entre carriles de una conexión con este mismo servicio.
   - **Turnos:** filas de `core_decisions` con `outcome` `routed` o `escalated` (las de palabra de escalamiento cuentan). No cuentan los bloqueos por política (pausa, cuota, horario).
   - **Escalamiento:** `outcome = 'escalated'` o `turn_outcomes.status = 'escalated'`. Se reporta por turno y también en conversaciones distintas; el canary cuenta su muestra en conversaciones.
   - **Ledger best-effort:** un turno sin `turn_outcomes` (la escritura del resultado falló) no cuenta como fallido ni escalado. Se reporta aparte como `sin_resultado`, para que el canary lo trate como señal y no como éxito.
   - **Dinero, de una sola fuente:** `usage_ledger` (filas `turn` y `agent_run`, sin `NO_ES_ACTIVIDAD`). La versión sale de `usage_ledger.release_id` y el carril, por `turn_id → core_decisions`. `turn_outcomes` queda para latencia y status.
   - **Operaciones entre agentes:** el costo de las filas `agent_run` se agrupa por (agente que pide, agente que responde, modo), uniendo `a2a_call_id` con `a2a_events`. El dinero viene del ledger.
   - **Rechazos del verificador por tipo** (`verdict = 'fail'`, incluidos los borradores que el reintento corrigió: es lo que el verificador frenó, no siempre lo que vio el cliente), menos los falsos positivos marcados.
   - **Completitud por procedimiento:** tareas cerradas en la ventana (`completed_at`), completadas sobre terminadas. La versión y el carril salen por `started_turn_id → core_decisions`.
   - **Muestra mínima:** con menos de 20 turnos, o de 10 tareas terminadas por procedimiento, la tasa sale como `null` y el panel dice «sin datos suficientes».
2. **Consistencia vigente** (`veredicto.consistencia_vigente`, una sola función). Para cada procedimiento de la versión activa vale el último veredicto de la cadena `base_release_id` (como mucho 10 versiones hacia atrás) que lo midió, siempre que en las versiones posteriores no se haya vuelto a tocar.
   - Si lo tocó una versión posterior sin medirlo, o la cadena se corta sin medición, sale «sin datos».
   - El número es `por_procedimiento.pass_k`: la fracción de escenarios estables en k = 4. `/evals/runs` expone el promedio de la corrida con otro nombre (`pass_k_promedio`) y nunca su costo, que es de la plataforma.
   - Aparecen todas las conexiones conversacionales: las que no tienen versión activa también, con «sin datos».
3. **Falsos positivos del verificador:** `turn_feedback(source = 'owner', signal = 'verifier_false_positive:<tipo>')`, con `note` libre para el dueño.
   - Exige escritura en `agents`.
   - Es idempotente con un índice único parcial y `ON CONFLICT DO NOTHING`.
   - No se desmarca: `turn_feedback` es append-only y sin borrado. Si hiciera falta, sería una señal inversa más adelante.
4. **API:**
   - `GET /api/v1/admin/reliability`: métricas, consistencia y completitud;
   - `GET /api/v1/admin/usage?group_by=connection|agent|a2a|day`;
   - `GET /api/v1/admin/evals/runs`;
   - `GET/POST /api/v1/admin/reliability/verifier[...]`: rechazos y la marca de falso positivo.

   «Calidad» y «Consumo» cuelgan de la sección `agents`; no se crea una sección nueva. Sin permiso de `costs`, los campos de dinero van en null. Sin permiso de `conversations`, la lista de rechazos no trae `conversation_id`.
5. **Migración concurrente** (como la 0071):
   - `core_decisions (tenant_id, release_id, created_at) WHERE release_id IS NOT NULL` (el tablero filtra por versión);
   - `conversation_tasks (tenant_id, completed_at) WHERE completed_at IS NOT NULL`;
   - el índice único parcial de los falsos positivos.
6. **Fuera de esta HU:**
   - que la consistencia de la consola de conexiones y de «Mi agente y su data» use `consistencia_vigente`: cambia sus contratos y su panel;
   - los errores de acción (`action_failed`) como señal dura, que llegan con HU-AOS-32;
   - las listas «frases que nadie reconoce» y «datos que el cliente tuvo que repetir»;
   - la vista master por negocio.
