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
