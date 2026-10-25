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
