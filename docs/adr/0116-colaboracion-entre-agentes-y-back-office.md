# ADR-0116 — Colaboración entre agentes en un hilo y back-office asíncrono

- **Estado:** aceptado (2026-10-09, HU-AOS-01)
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-18, HU-AOS-19, HU-AOS-20 y HU-AOS-30 (SPEC-26). Usa ADR-0112, ADR-0114, ADR-0113
  (`exposes.consultas`, límites del turno en el KernelSpec), ADR-0117 (el grafo va dentro de la release), ADR-0107
  (outbox), ADR-0108 (plantillas por evento), ADR-0122 (pausa) y ADR-0008 (`usage_ledger`).

## Contexto

- El Agent OS exige cinco modos de colaboración entre agentes (delegación, handoff, consulta, broadcast y colaboración).
- Cada modo necesita grafo de permisos, límites de profundidad, tiempo y presupuesto, trazabilidad total (de, a, intención, payload, resultado, tokens, costo, latencia) y aislamiento absoluto entre negocios.
- Hacerlo con agentes LLM independientes frente al cliente parte el contexto (ADR-0112).

## Decisión

Los agentes colaboran en el mismo hilo con mecanismos deterministas, permisos versionados y un ledger append-only; lo asíncrono corre en el worker y solo propone.

1. **Modos en el mismo hilo**, con un solo estado:
   - **Handoff:** cambio de dueño de la conversación, aplicado al cerrar el turno (dos fases). Las tareas viajan intactas.
   - **Delegación:** "agente como acción". `encargar(agente, procedimiento, entradas)` crea una sub-tarea del procedimiento del otro agente; si las entradas bastan, la completa el motor sin LLM; el resultado es tipado.
   - **Consulta:** lectura tipada que el otro agente declara en `exposes.consultas`, casi siempre sin LLM.
   - **Broadcast:** el Coordinador puntúa los disparadores de todos los agentes; el LLM solo desempata.
   - **Colaboración:** pila de tareas (una en foco y otra en pausa) resuelta en un solo mensaje, con una pregunta como máximo.

   Broadcast y colaboración son mecanismos internos del Coordinador, no llamadas entre agentes: no van en `agent_links.modes`; basta con que la instancia esté habilitada en la conexión.
2. **Permisos versionados:**
   - `a2a_graphs`, `a2a_graph_versions` y `agent_links`, con scope de negocio. El scope de conexión (con "un `deny` de conexión gana") se agrega cuando un negocio con dos conexiones pida permisos distintos.
   - Nadie llama al Coordinador.
3. **Guarda determinista** de cada operación:
   - permiso y contrato expuesto;
   - profundidad y ciclos (0);
   - repetición (misma operación y payload dos veces en un turno);
   - operaciones por turno y cambios de dueño (pasado el límite, se congela al dueño);
   - clases de datos;
   - presupuesto con reserva previa y liquidación posterior.

   Los límites tienen una sola fuente: los del turno en el KernelSpec (por defecto: profundidad 3, 6 operaciones por turno y más de 2 cambios de dueño en 6 turnos congela al dueño), los de cada enlace (modos, intenciones, tiempo, presupuesto, clases de datos) en `agent_links`, que solo puede bajarlos. La validación de la release exige `agent_links.max_depth` ≤ el límite del KernelSpec.
4. **Ledger:**
   - `a2a_events` es append-only, con payload redactado y su hash (nunca el payload completo). Se conserva hasta `purge_tenant`: una retención menor requiere un ADR que la declare como excepción a la invariante 8.
   - `a2a_conversations` es la proyección (estado, gasto, profundidad), actualizada en la misma transacción que el evento y reconstruible desde `a2a_events`.
   - La API arma una fila por llamada agrupando por `call_id`. No se crea una vista con `tenant_id` en `public`: `purge_tenant` recorre `information_schema.columns` y la trataría como tabla (`alembic/versions/0011_purge_tenant.py`).
   - Todas las tablas llevan `tenant_id`, RLS forzada con policy e índices que empiezan por `tenant_id`, y **toda** referencia a otra entidad del negocio (conexión, conversación, turno, instancia, versión) es **FK compuesta `(tenant_id, …)`**: un enlace o un evento entre negocios es imposible.
   - El gasto se registra en `usage_ledger` (ADR-0008) con `a2a_call_id` y cuenta para `costo_llm_usd` del plan (ADR-0045); `a2a_events` no es fuente de facturación.
   - Lo que vuelve de otro agente es dato validado contra un schema, nunca instrucción (invariante 7).
5. **Back-office:**
   - Son trabajos del worker disparados por el outbox (ADR-0107) o por crons (ADR-0044). Usan el LLM solo donde aporta (resumen, juez).
   - `core/backoffice/` solo da el runner: disparo, presupuesto, ledger y salida como propuesta. Los trabajos con lógica de dominio viven en su pack; el core no conoce pedidos.
   - Los prompts de los trabajos que usan el LLM viven en el AgentSpec del agente `kind = backoffice` o en playbooks del pack, nunca en `core/backoffice/` (invariante 3).
   - El presupuesto mensual es `policies.backofficeBudgetUsdMonthly` del KernelSpec (ADR-0113), versionado con la release (ADR-0117), y solo baja el tope `costo_llm_usd` del plan (ADR-0045).
   - **Solo proponen.** Una propuesta es un evento del outbox que el Coordinador de la conexión procesa como un turno sin mensaje entrante, con la release de la conversación (ADR-0117). Fuera de 24 h solo sale plantilla por evento (ADR-0108). Con la conexión pausada queda `failed`, sin reintento ni escalamiento (ADR-0122 §2), y el trabajo no corre ni gasta LLM sobre conexiones pausadas.
   - Los crons de packs que hoy escriben al cliente (`pack_commerce.jobs.follow_up_leads` y `follow_up_quotes`) pasan a proponer por este camino en HU-AOS-30. Hasta entonces son la excepción conocida.
   - Un agente de varios pasos con contexto propio se suma cuando un caso real lo exija.
6. La **federación entre negocios** queda fuera (SPEC-27): requiere doble consentimiento y un gateway propio.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Agentes con contexto propio conversando frente al cliente | Inconsistencia por contexto partido |
| Cada operación como tarea de Celery | Suma latencia por salto y pierde el contexto del turno |
| Solo trazas OTel como registro | Retención y consultas insuficientes para auditoría; la facturación sigue en `usage_ledger` |
| Vista `a2a_calls` en `public` | Rompe `purge_tenant` para todos los negocios |

## Consecuencias

**Positivas:** colaboración trazable y acotada; cero bucles ejecutados por construcción; aislamiento entre negocios garantizado por el esquema.

**Negativas / lo que aceptamos pagar:** menos libertad para que un agente "converse" con otro; más tablas e índices; hay que calibrar los límites.

**Qué haría que revisáramos esto:** una tasa alta de denegaciones legítimas (los límites cortan casos válidos), un caso que exija contexto propio frente al cliente, o un trabajo de back-office que necesite varios pasos con contexto propio.

## Impacto en el código

`core/kernel/a2a.py` (guarda y operaciones); comandos `pasar_a_agente`, `encargar` y `consultar`; `core/backoffice/` (runner); `packages/pack-commerce/pack_commerce/jobs.py` (seguimientos como propuesta, HU-AOS-30); migraciones de permisos, ledger y proyección.

## Adenda HU-AOS-18 (2026-10-25): guarda, grafo congelado en la release y ledger

**Estado:** propuesta (revisada por el arquitecto). Decisiones del dueño del 2026-10-25: (a) el alcance es la guarda, el grafo versionado, el ledger y la API mínima; `encargar`, `consultar` y `pasar_a_agente` llegan con la HU que los use (el handoff es HU-AOS-19); (b) el «congelado del dueño» reutiliza el freno de estabilidad de HU-AOS-17; (c) `payload_hash` es un HMAC con una clave derivada de `APP_SECRET_KEY`.

1. **Grafo versionado** (`core/agents/grafo_a2a.py`, junto a la publicación de versiones de agentes):
   - Un `a2a_graphs` vivo por negocio, creado al publicar la primera versión.
   - `publicar(tenant, enlaces, created_by)` corre bajo `pg_advisory_xact_lock` por negocio (la primera versión no tiene fila que bloquear y dos altas concurrentes chocarían con `uq_a2a_graphs_tenant`). Valida cada enlace (modelo `EnlaceA2A` del core, no de `agentcore_contracts`: los enlaces son filas, no contrato de agente), calcula `content_hash` (sha256 del JSON canónico de los enlaces ordenados por `(from, to)`) y, si coincide con la última versión, devuelve esa (idempotente). Si no, inserta la versión `n+1` y sus `agent_links`. `agent_links` es append-only: editar = versión nueva.
   - `modes` solo acepta `delegate`, `handoff` y `query`: `broadcast` y `collaborate` dan 422 `modo_no_permitido` (§1); el CHECK de la base queda de respaldo. `from == to` da 422 `autollamada`. Los agentes deben ser del negocio y no estar borrados (la FK compuesta lo garantiza entre negocios).
   - Un ciclo de `handoff` en el grafo (A→B→A) no se rechaza al guardar (devolver el control es legítimo): la API lo devuelve en `advertencias` para el panel (HU-AOS-26). En runtime, la guarda corta los ciclos.
   - Publicar no construye releases: aplica con la próxima release de cada conexión (como editar un agente). `migrar-a-v3` verá las releases activas como desactualizadas hasta correrlo con `--construir`.
   - Los presets (deuda de ADR-0139) **no escriben grafo** mientras no exista una operación A2A que lo use: sería configuración sin consumidor. La deuda pasa a la HU de `encargar`/`consultar`.
2. **Congelado en la release** (sigue siendo `release/2`: la clave nueva es opcional y `cargar.py` ignora las que no conoce):
   - `armar_manifiesto` lee la última versión del grafo y, **solo si existe**, agrega `a2a_graph: {version_id, version, content_hash, links}`. Entra al hash: cambiar los permisos produce una release nueva y la dedupe por `manifest_hash` no la esconde. Sin grafo la clave no aparece y el hash de las releases existentes no cambia; una release sin grafo significa «sin enlaces» (todo se deniega con `no_link`, como hoy).
   - `construir` escribe `connection_releases.graph_version_id` **desde el manifiesto** (no relee la última versión: una publicación entre el armado y el INSERT los desalinearía). Un rollback vuelve al grafo de la release anterior.
   - `manifiesto.resumen()` muestra versión, hash y número de enlaces.
   - `revisar_a2a` falla el build con `a2a_invalido` si un enlace entre dos agentes de la conexión pide más que el KernelSpec del Coordinador: `max_depth > a2a.maxDepth`, `budget_usd > a2a.turnBudgetUsd` o `budget_tokens > a2a.turnBudgetTokens` (el enlace solo puede bajar los límites, §3). Los enlaces con un agente fuera de la conexión quedan inertes: ni error ni revisión.
   - El gate de pass^k (HU-AOS-21) no mira `a2a_graph`: hoy no hay operaciones y un cambio de permisos no cambia ninguna conducta. Cuando lleguen `encargar` o el handoff, un cambio de enlace cuenta como tocados los procedimientos de los agentes `from` y `to`.
   - En runtime, la guarda lee el grafo de `ReleaseCongelada.a2a`, nunca de las tablas vivas.
3. **Guarda** (`core/kernel/a2a.py`). `evaluar(estado, operacion, grafo, limites, cambios_de_dueno)` es pura y determinista (sin LLM ni base): recibe ya calculados los cambios de dueño que leyó el router en el turno, igual que `estabilizar`. Revisa en este orden y devuelve la primera razón:
   - `turn_closed`: el turno ya quedó en `loop_blocked` o `budget_exhausted` (todo lo que sigue se deniega);
   - `cycle`: `to` ya está en la pila de llamadas del turno, incluida la autollamada `from == to`. Va antes que el permiso: un bucle es un bucle haya o no enlace (una autollamada nunca tiene enlace, `ck_agent_links_self`), y así deja la conversación en `loop_blocked` y avisa;
   - `no_link`: no hay enlace `from → to` en el grafo congelado, el modo no está en `modes` o la intención no está en `intents` (lista vacía = cualquiera). El «contrato expuesto» (`exposes.consultas` o el procedimiento del destino) llega con `encargar`/`consultar`;
   - `depth`: la profundidad pasaría `min(enlace.max_depth, a2a.maxDepth)`;
   - `repeat`: la misma `(from, to, modo, intención, payload_hash)` ya se pidió en el turno;
   - `calls`: el turno llegó a `a2a.maxCallsPerTurn`. **Las denegaciones también cuentan**: un LLM que reintenta lo denegado se topa con el límite;
   - `owner_frozen`: solo en `handoff`, cuando los cambios de dueño son `≥ routing.owner.maxOwnerChangesPer6Turns` (misma fuente que el router, sin duplicar el límite en `a2a`). Si el conteo no se pudo leer (`None`), el handoff se deniega: al revés que el ruteo, cambiar de dueño no es el camino seguro;
   - `data_class`: la operación declara una clase de datos que el enlace no permite;
   - `budget`: la reserva (el `budget_usd`/`budget_tokens` completo del enlace, sin estimar) pasaría lo que le queda al turno de `a2a.turnBudgetUsd`/`turnBudgetTokens`.

   Con permiso, la operación queda `accepted` con su reserva; `liquidar` escribe `completed`/`failed` con el gasto real y libera lo no gastado. El estado del turno (pila, conteo, repetidos, gasto, cerrado) vive en memoria del turno y se reconstruye desde `a2a_events` del turno si hace falta.
4. **Ledger:**
   - Requiere la cabecera del turno en `core_decisions` (`a2a_conversations.turn_id` tiene FK y el CHECK `trigger = 'turn'` la exige). Sin cabecera confirmada (`decidir()` es best-effort o `KERNEL_LEDGER_ENABLED=false`), la operación se deniega sin escribir nada y queda en el log.
   - `registrar` usa **su propia transacción** (un turno que falla no se lleva la auditoría de las denegaciones). La conversación A2A se crea en la misma transacción que su primer evento (`seq = 1`, `deadline_at = now() + timeout_ms` del enlace de la primera operación, o 15 s si fue denegada sin enlace); los siguientes eventos toman `seq` bajo `FOR UPDATE` de la proyección y actualizan `a2a_conversations` (estado, `calls`, `depth_reached`, `spent_*`) en la misma transacción. Un `cycle` deja la conversación A2A en `loop_blocked` y un `budget`, en `budget_exhausted`.
   - La intención es un identificador (`^[a-z0-9_.:-]{1,80}$`): otra forma se niega con `no_link` y se guarda como `invalida` (puede venir del LLM con texto del cliente). Si quien pide no es una instancia de la release, se niega sin escribir (`no_ledger`); un destino ajeno se registra con `to_instance_id` nulo.
   - El evento lleva `depth` = la profundidad de la operación pedida, acotada a 5 (el CHECK es `BETWEEN 1 AND 5`); un rechazo por `depth` en el límite registra 5 con `reason = 'depth'`.
   - `a2a_events.reason` pasa a tener CHECK con las razones de §3 más `no_ledger`, `timeout` y `error` (migración aditiva `NOT VALID` + `VALIDATE`).
   - Un `cycle` emite además `a2a.loop.detected` en el bus in-process después de confirmar, con `tenant_id`, `connection_id`, `call_id` y la pila de instancias, sin payload.
   - `payload_hash` = HMAC-SHA256 del payload canónico con la clave derivada `hmac(APP_SECRET_KEY, "a2a.payload_hash.v1" + tenant_id)` (por negocio: el mismo payload no se puede cruzar entre negocios). `payload_redacted` guarda solo la forma (claves y tipos), con profundidad máxima 3, hasta 20 claves por nivel y claves de hasta 40 caracteres; las claves que parecen datos personales (dígitos largos, `@`) se reemplazan por `«clave1»`, `«clave2»`…. Rotar `APP_SECRET_KEY` hace que los hashes nuevos no se comparen con los viejos (aceptado: sirve para detectar repetidos dentro del turno y auditar igualdad, no para firmar a largo plazo).
   - El gasto LLM de una operación va a `usage_ledger` con `kind = 'agent_run'`, `a2a_call_id`, `agent_instance_id` = la instancia `to` y `conversation_id`, `connection_id`, `release_id` y `turn_id` del turno padre, en la misma transacción que su evento `completed`/`failed`. Es gasto del negocio y del agente (cuenta para `costo_llm_usd`, para «Mi agente y su data» y para los rankings de costo) pero no es un turno. `NO_SON_TURNOS` se separa en dos: `NO_SON_TURNOS` (contar turnos: suma `agent_run`) y `NO_ES_ACTIVIDAD` (actividad y gasto por agente/conversación: `embedding`, `generacion`, `eval`).
5. **API** (`/api/v1/admin/a2a`, sección `settings` como los agentes; escribir con la sección en escritura y el candado `aos_runtime_legacy` de las versiones; la exigencia de propietario o master queda en promover la release, que es lo que llega a clientes reales):
   - `GET /graph`: versión vigente, enlaces y advertencias;
   - `PUT /graph`: publica una versión (idempotente por hash), 422 con los códigos de §1;
   - `GET /graph/versions`: historial;
   - `GET /calls?connection_id&limit&before`: una fila por `call_id` (agrupando `a2a_events`), sin payload.
6. **Fuera de esta adenda:** las operaciones reales (`encargar`, `consultar`, `pasar_a_agente`) con su contrato expuesto, el scope de conexión del grafo (§2), el timeout por turno, el grafo de los presets y el congelado persistente del dueño (si el freno por turno no basta, se revisa).

## Adenda HU-AOS-19 (2026-10-25): handoff, encargo y consulta en el mismo hilo

**Estado:** propuesta, revisada por el arquitecto. Decisiones del dueño del 2026-10-25: (a) `encargar` solo acepta procedimientos que se completan con datos; ejecutar la acción de salida sin LLM queda para después; (b) una consulta nombra una acción de lectura (`ExposedQuery.action`, contratos 3.2.0, adenda de ADR-0113). Se entrega en dos partes: **parte 1**, el handoff (§1-§3); **parte 2**, el encargo y la consulta (§4-§5).

1. **Tres comandos del hilo** (`core/kernel/procedures/comandos.py`, textos en el playbook `comandos`), con la misma infraestructura que `actualizar_tarea`: el modelo propone, el motor decide, nunca lanzan y devuelven `{ok, state_card, errors, …}`. Los nombres entran en `NOMBRES`, `_ARGUMENTOS` y `TEXTOS` (fail-closed: si falta un texto, el playbook no carga).
   - `pasar_a_agente(agente)`;
   - `encargar(agente, procedimiento, entradas)`, solo con tarea en foco;
   - `consultar(agente, consulta, parametros)`.

   `agente` es un enum con las **keys** de los especialistas de la release que el grafo congelado deja alcanzar desde el agente del turno con ese modo. El modelo nunca ve ids: el kernel traduce key → instancia con los componentes de la release. Se ofrecen solo con el motor activo, release `release/2` y al menos un destino para el modo; `pasar_a_agente` y `consultar` no dependen del foco. **Sin grafo, el prompt queda igual que hoy.**
2. **Contexto A2A del turno.** `ArtefactosDeRelease` suma `a2a_graph` (el del manifiesto, datos planos). `contexto_del_motor` arma una vez por turno el `GrafoCongelado`, los `Limites` (del KernelSpec congelado) y un `EstadoDelTurno`, que comparten todas las operaciones del turno. Los cambios de dueño se leen con `router.cambios_de_dueno` en el momento del handoff, la misma fuente que el router.
3. **Handoff en dos fases** (`core/kernel/handoff.py`).
   - **Destinos posibles:** especialistas con enlace `handoff` desde el agente del turno que **declaran el procedimiento de cada tarea abierta** de la conversación. Si no lo declaran, no aparecen en el enum y la guarda los niega con `no_link`: es el contrato expuesto, sin razón nueva. Así una tarea nunca queda con un dueño que no la puede gobernar.
   - **Fase 1 (en el turno):**
     - La guarda registra la operación `handoff` (`accepted` o la negación) y libera la reserva enseguida (no hay gasto LLM).
     - Si se acepta, el `Turno` guarda el handoff pendiente con todo lo que la fase 2 necesita sin leer la release: `from` y `to` (instancias), `call_id`, `a2a_conversation_id`, `connection_id` y `conversation_id`.
     - Un segundo `pasar_a_agente` en el mismo turno no se ofrece.
     - El modelo recibe que el pedido queda con el destino desde el siguiente mensaje y puede despedirse en esta respuesta. El texto no lo promete de forma absoluta: si el cliente vuelve pasado `idleMinutes`, el episodio nuevo suelta al dueño; y sin tarea en foco, una regla o un disparador del router puede llevar el turno a otro agente.
     - Con un handoff pendiente, `cerrar_turno` no cierra la conversación A2A: la cierra la fase 2.
   - **Fase 2 (al cerrar el turno):** la llama `_abrir_turno` después del `yield` y antes de escribir `turn_outcomes`, en el `tenant_scope` del turno (comprueba `get_current_tenant_id() == turno.tenant_id`). Es el único punto posterior a la entrega que conoce el status final. Reglas:
     - Solo si el turno terminó `replied` o `silent`. Con `neutral`, el guardrail reemplazó la respuesta y el cliente no vio la despedida.
     - Tiene un solo presupuesto de tiempo para aplicar y, si falla, cancelar (2 s, como las escrituras del ledger del turno) y **nunca lanza**: si lanzara, el consumer reintentaría el turno y el cliente recibiría la respuesta dos veces. Un fallo queda en el log y como `cancelled` (best-effort).
     - Todo va en una transacción, bajo `FOR UPDATE` de la conversación:
       1. Comprueba que `active_agent_instance_id` siga siendo el `from` y que no haya tareas abiertas que no estaban al aceptar (el destino podría no gobernarlas; con un handoff pendiente, además, el turno no inicia tareas nuevas). El turn gate es por negocio, no por conversación, y dos turnos de la misma conversación pueden cruzarse. Si cambió, `cancelled`.
       2. `active_agent_instance_id` = destino y `owner_since = now()`.
       3. `owner_changed` (`from_instance_id`, `to_instance_id`, actor `kernel`) en cada tarea de `state.tareas_abiertas`, que excluye las sub-tareas (estas nunca quedan abiertas: se completan en su transacción). Las tareas viajan intactas: misma tarjeta, datos y etapa. Con `aos_routing = active`, el router las sigue con el nuevo dueño (`source = task`); con `off`, el foco es el dueño de la conversación.
       4. Evento A2A `handoff_applied` y cierre de la conversación A2A como `completed`.
     - `turn_outcomes.final_instance_id` = destino, solo si la transacción hizo commit.
   - **Si no se aplica:** con un turno `failed`, `escalated` o `neutral`, o con el dueño cambiado por otro turno, el ledger escribe `cancelled` (con `reason` nulo, o `error` si falló la escritura) y la conversación A2A queda `failed`. Un turno cancelado por `TurnTimeout` (`CancelledError`) no llega a la fase 2: el `accepted` queda sin cierre en el ledger y el dueño no cambia. Lo aceptamos: la proyección queda `open` con `deadline_at` vencido, y eso se puede reconocer.
   - Si el tope corta justo después del COMMIT, el dueño queda cambiado y el ledger dice `cancelled`. Es una inconsistencia de auditoría aceptada: el dueño real es el de la conversación.
   - **Ping-pong:** la guarda niega con `owner_frozen` cuando los cambios de dueño de las últimas 6 decisiones ya están en `routing.owner.maxOwnerChangesPer6Turns`.
4. **Encargo (parte 2)**: una sub-tarea completada sin LLM.
   - **Encargable:** el destino lo declara en `exposes.procedures` y lo publica en la release; la definición es la congelada del destino (`procedimientos_por_agente`), no la del registro. Además, el procedimiento **se completa con datos**:
     - ninguna etapa tiene acción de salida, confirmación, datos `confirmed` en la salida ni precondiciones, y no espera eventos de cierre;
     - todo dato que pide una salida es escribible (sin `setBy`, sin `location`/`media`, sin `candidates`);
     - ningún dato tiene `confirm: true` ni es de clase `payment`.
     
     Lo que no cumple no aparece en el enum (fail-closed). Hoy ningún procedimiento de los packs es encargable: los escenarios usan un procedimiento de prueba y no se construye editor ni preset para esto.
   - **`entradas`:** un objeto con la unión de los datos escribibles de los procedimientos ofrecidos. El motor valida contra el procedimiento elegido (un dato que no es suyo es un error).
   - **Ejecución:**
     - La guarda registra `delegate`: la intención es el id del procedimiento y las clases de datos, las de los datos enviados.
     - Si se acepta, se comprueba que el pack del destino siga sano.
     - Después, en **una transacción bajo el lock de la conversación**:
       1. crea la sub-tarea (`parent_task_id` = tarea en foco, `owner_instance_id` = destino);
       2. valida las `entradas` con `_validar_dato` (resolvers con el contexto del destino y su presupuesto de tiempo), `_no_dichos` (lo que el cliente no dijo se rechaza) y `_correos_dudosos` (un correo dudoso hace fallar el encargo y el modelo pregunta). Todo o nada;
       3. fija los datos y avanza las etapas de forma pura (`motor.salida_cumplida` + `motor.siguiente`, con tope `_MAX_AVANCES`), sin `_avanzar` ni `_devolver_control`, hasta `completed`.
   - **Si no se completa** (falta un dato), la transacción se deshace: no queda sub-tarea a medias, el modelo recibe qué falta y el ledger escribe `failed`.
   - **Efectos externos:** `procedure.slots_set` se emite solo después del commit y solo con la sub-tarea completada. `datos_fijados` (evidencia del verificador sobre la tarea en foco) no cambia.
   - **Resultado tipado:** los datos finales de la sub-tarea, sin los `pii` si el enlace no permite `pii`. Va como dato JSON (invariante 7) y se registra solo su forma.
5. **Consulta (parte 2):**
   - **Consultable:**
     - el destino declara la consulta con `action` y la acción está entre sus `tools` y en el scope de la release;
     - la acción **no aparece en `stage.actions` de ningún procedimiento de la release** (no solo las puertas): si no, `consultar` ejecutaría fuera de etapa una acción que gobierna el motor y se saltaría el ligado y la confirmación;
     - la acción no pide confirmación en el binding.
     
     Si llega una consulta que no cumple, se niega.
   - **Ejecución:**
     - La guarda registra `query`. La intención es el id de la consulta y la clase es `internal`; los `parametros` son datos para la acción, no se clasifican, y el chequeo de clases del enlace no los cubre.
     - La acción corre con un ejecutor propio de consulta: el spec, el binding y la identidad del **destino** (denegaciones y confirmación del destino), memoria de solo lectura (`write_namespaces` vacío), auditoría como cualquier acción y clave idempotente `query-{call_id}`.
     - La salida se valida contra `outputSchema` con el subconjunto de JSON Schema del kernel.
   - **Resultado:** si valida, vuelve como dato (`resultado`); si no, `ok: false` y el ledger escribe `failed`.
6. **Gasto:** estas operaciones no usan LLM propio y no escriben `agent_run`; `liquidar` se llama sin gasto. El tiempo y el costo LLM siguen en la fila `turn` del turno.
7. **Escenarios simulados sin LLM** (modelo guionado, como HU-AOS-17):
   - **Parte 1:**
     - handoff sin pérdida: el segundo turno lo atiende el destino con `aos_routing` `off` y `active`, con la misma tarea, etapa y datos, `owner_changed` y `handoff_applied`;
     - ping-pong en tres turnos (A→B pasa, B→A pasa, el tercero sale `owner_frozen`);
     - destino que no declara la tarea en foco (no se ofrece y se niega);
     - turno que escala o falla después de la fase 1 (sin cambio y `cancelled`);
     - dueño cambiado por un turno concurrente (`cancelled`);
     - sin grafo, el prompt es idéntico.
   - **Parte 2:**
     - encargo que se completa, con resultado tipado;
     - encargo con un dato no dicho (rollback, sin sub-tarea y sin `slots_set`);
     - consulta válida;
     - salida que no valida el schema;
     - consulta a una acción de etapa (no ofrecida y negada).

   Los journeys con LLM real quedan para cuando el dueño apruebe el costo.
8. **Fuera de esta adenda:** que el motor ejecute la acción de salida de un encargo; la pila de colaboración (HU-AOS-20, con `maxStack`); que un cambio de enlace cuente como procedimientos tocados en el gate (adenda HU-AOS-18 §2); y los presets con grafo.
