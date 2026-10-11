# ADR-0112 — Agent OS de procedimientos: Coordinador por conexión, un hilo por conversación y agentes especialistas

- **Estado:** aceptado (2026-10-09, HU-AOS-01)
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-01 (SPEC-26 en `solvershub-docs`, decisiones D-27 y D-28). Responde la pregunta P-10.
  No cambia la topología (rige ADR-0021). Los contratos cambian en ADR-0113.

## Contexto

- Hoy hay **un agente por negocio**: un solo `AgentSpec` activo (`core/agents/spec.py`), que el loop carga sin conexión (`load_active_spec()` en `run_turn`, `core/orchestrator/loop.py`); un playbook decide entre vender o agendar (`core/playbooks/enrutamiento.md`).
- El negocio quiere vender un sistema operativo de agentes: N conexiones por negocio y varios agentes especialistas por conexión que colaboran.
- El problema principal es la **consistencia**, no la cantidad de agentes: la línea base de evals quedó en 34/35 y 7/8, tres guardrails heurísticos escalaban compras válidas (ADR-0119 retiró dos; `calendar_completion_check` sigue en medición) y las secciones del flujo no restringen qué acción se llama. Desde HU-ARCH-28 la consistencia se mide con pass^k.
- La evidencia externa va en el mismo sentido: partir el contexto entre agentes produce decisiones incompatibles (Cognition, jun-2025); el multiagente gasta ≈ 15× los tokens de un chat y encaja mal cuando las tareas comparten contexto (Anthropic, jun-2025); los agentes de atención al cliente son inconsistentes entre intentos (τ-bench, jun-2024).

## Decisión

Cada conexión tiene un Coordinador que ejecuta procedimientos tipados sobre un estado único, y los agentes especialistas atienden en el mismo hilo.

1. **La conexión es la unidad de ejecución.** Cada conexión conversacional tiene **exactamente un Coordinador** (el Agente Core: un kernel determinista) y N agentes especialistas. Hay una conversación abierta por (conexión, cliente) (ADR-0115).
2. **El Coordinador ejecuta procedimientos tipados sobre un estado único** (ADR-0113 y ADR-0114). El LLM entiende y redacta; el kernel decide y actúa (datos validados, acciones permitidas por etapa, confirmación, efectos idempotentes); un verificador comprueba lo que se dice (ADR-0119). El LLM es coprocesador del kernel, no su dueño.
   Una conexión pausada (ADR-0122) no ejecuta el Coordinador: el mensaje se guarda sin IA, como hoy.
3. **Los especialistas son agentes del mismo hilo**, con una voz por conexión. La colaboración entre agentes (handoff, delegación, consulta, broadcast y colaboración) usa mecanismos deterministas (ADR-0116).
4. **Lo asíncrono va aparte.** Seguimiento, conciliación, resúmenes y auditoría corren en el worker y solo proponen (ADR-0116; los seguimientos de `pack_commerce.jobs` que hoy escriben directo al cliente son la excepción conocida hasta HU-AOS-30). Un agente con contexto propio se suma ahí cuando un caso real lo exija.
5. **Monolito modular**, el mismo binario: módulos nuevos `core/kernel/`, `core/releases/`, y `core/backoffice/`. No hay microservicios.
6. **Topología sin cambios.** Rige ADR-0021: si su disparador se cumple, se separan `panel` y `gateway+consumer` por `AGENT_CORE_ROLE`. El Coordinador corre donde corre el consumer, y el back-office en el worker.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Especialistas LLM independientes conversando entre sí frente al cliente | El error se compone por salto y el contexto se parte; es la causa de inconsistencia documentada; además, más tokens y latencia |
| Microservicios (gateway, kernel, runtime, broker) | Un salto de red por operación entre agentes y una operación que 1–3 personas no sostienen |
| Reescribir sobre un framework de agentes | Pierde las invariantes, el RLS, los golden sets y las habilidades existentes |
| Seguir con un agente y playbooks | No mejora la consistencia ni permite vender varios agentes por conexión |

## Consecuencias

**Positivas:**
- Varios agentes por conexión con una sola voz y un solo estado: el cliente no repite datos.
- La lógica de negocio queda en código y la consistencia se mide (pass^k).
- Reutiliza el loop, el RLS, el outbox, los ledgers y el panel.

**Negativas / lo que aceptamos pagar:**
- Contratos nuevos (major, ADR-0113).
- Autoría de procedimientos por habilidad.
- Las HU nuevas de SPEC-26 en el backlog (`tasks.md` de SPEC-26).
- La "autonomía" frente al cliente es menor que con agentes independientes.

**Qué haría que revisáramos esto:**
- Que pass^k no mejore frente a la línea base de un agente: el pass^4 que mide HU-ARCH-28 en el nocturno con k = 4 antes de encender `AOS_PROCEDURES` (valor y fecha se anotan en la bitácora de SPEC-26).
- Que los procedimientos frustren conversaciones naturales: frases sin disparador o datos repetidos en aumento.
- Que un caso real exija agentes con contexto propio frente al cliente.

## Impacto en el código

- Módulos nuevos:
  - `core/kernel/`: `ingress`, `router`, `procedures`, `state`, `a2a`, `egress`, `decisions`. El verificador sigue en `core/orchestrator/verificador/` (ADR-0119) y suma la evidencia de las tareas; no hay un segundo verificador;
  - `core/agents/runtime.py` y `compiler.py`;
  - `core/releases/` y `core/backoffice/`;
  - `CLAUDE.md` (Estructura) y `docs/architecture/c4.md`.
- `core/orchestrator/consumer.py` y `loop.py` se refactorizan detrás de banderas (`AOS_PROCEDURES`, `AOS_CONNECTION_ROUTING`, `AOS_RUNTIME`), sin cambio de conducta hasta encenderlas.
  - `AOS_PROCEDURES` y `AOS_RUNTIME` son configuración de plataforma **por negocio**, guardada en base de datos y editable solo por el master: se encienden negocio por negocio sin redeploy.
  - `AOS_CONNECTION_ROUTING` es global por entorno, porque su *contract* (ADR-0115) es global.
  - Orden de encendido: `AOS_CONNECTION_ROUTING` (global, encendida/apagada) → `AOS_RUNTIME` (`legacy` | `v3`, por negocio; `v3` exige `AOS_CONNECTION_ROUTING` encendida) → `AOS_PROCEDURES` (por negocio; exige `AOS_RUNTIME = v3`). Se apagan en orden inverso: apagar `AOS_CONNECTION_ROUTING` exige `AOS_RUNTIME = legacy` en todos los negocios. El panel del master rechaza las combinaciones inválidas.

## Adenda de implementación (2026-10-10, HU-AOS-10): ledger del Coordinador y trazas

Revisada por el agente `arquitecto` antes de escribir código. Es la primera parte de HU-AOS-10; no cambia contratos,
invariantes ni banderas de conducta.

- **Qué es un turno:** lo que el Coordinador hace con lo que entró: bloquearlo por una política, escalarlo o
  enrutarlo al hilo del agente. Deja dos filas append-only:
  - `core_decisions` (cabecera, `id = turn_id`) **al decidir**, antes de cualquier efecto del turno (respuesta fija,
    escalamiento, LLM). Las políticas del preámbulo deciden `blocked` (`kill_switch`, `escalated_conversation`,
    `quota`, `budget`, `connection_paused`, `out_of_hours`) o `escalated` (`escalation_keyword`); lo demás se decide
    `routed` (`route_source = default`, sin router todavía) justo antes de la primera llamada al LLM, así un
    presupuesto agotado en la primera vuelta queda `blocked/budget`.
  - `turn_outcomes` al cerrar, después de la entrega: `replied` (encolado, no entregado), `neutral` (ADR-0092),
    `escalated` (también si el envío falla y se escala), `silent` o `failed` (excepción; una tarea cancelada no
    escribe). Lleva tokens y costo del turno y el veredicto del verificador (`[{kind, verdict, count}]`, sin texto).
  - Agendar un debounce no es un turno: decide el disparo. El reintento de un inbound que falló no genera turno nuevo
    (el inbound ya está duplicado). El playground (canal `test`) escribe filas; el tablero (HU-AOS-29) lo filtra por
    canal.
  - **Sin fila** (no hay conversación y `conversation_id` es NOT NULL): negocio no operativo en el consumer o en el
    disparo del debounce, `QuotaExceeded` al crear el cliente o la conversación, y conversación inexistente.
- **Costo:** dos INSERT por turno y ninguna lectura extra (la conexión sale del objeto en memoria, los tokens del uso
  que ya se acumula para `usage_ledger`). Medido en local: p95 de 8,4 ms por turno para las dos escrituras.
- **`turn_id` en las filas del turno:** `messages`, `usage_ledger` (y `trace_id`) y `audit_log` lo toman por default
  de columna desde `core/shared/turno.py`, que solo se fija con la cabecera ya commiteada (las FKs son diferidas al
  COMMIT, pero cada sesión es su propia transacción). Significa "causado por el turno", no "de la conversación del
  turno". **Guarda de negocio:** si la fila es de otro negocio (cobro de plataforma, packs que cambian de
  `tenant_scope`), queda NULL; la FK compuesta nunca hace fallar una escritura que antes funcionaba. El inbound se
  guarda antes de decidir y queda NULL: lo enlaza `core_decisions.inbound_message_ids`. **Pendiente:** los ids del
  inbound en el camino debounce (la ráfaga vive en Redis como texto) y el `turn_id` dentro de las tareas Celery
  (`run_async` corre la corrutina en otro hilo y el contexto no viaja).
- **Trazas:** el turno abre `kernel.turn` como span actual; el hilo (`turn`, `llm_call`), las acciones
  (`tool_execute`) y la salida comparten `trace_id`. Con OTel apagado se adjunta un span no grabado con
  `trace_id = turn_id`. A Celery viaja en `traceparent` (señales `before_task_publish`/`task_prerun`/`task_postrun`,
  `core/observability/celery_trazas.py`), sin dependencias nuevas.
- **Best-effort mientras el runtime sea legacy:** un fallo de escritura se loguea y el turno sigue igual, también en
  latencia (cada INSERT tiene un tope de 2 s). Desde `AOS_PROCEDURES` el design (§5.3) pide fail-closed; llega con el
  motor de procedimientos.
- **Turno de un solo negocio:** el turno abierto solo es visible bajo el `tenant_scope` de su negocio; un turno de otro
  negocio anidado abre el suyo. El span `kernel.turn` no registra la excepción (su mensaje puede repetir datos del
  cliente), solo `error.type`. A Celery solo viaja W3C TraceContext, nunca baggage. Una tarea que el turno lance con
  `create_task` hereda su `turn_id` (mismo negocio): es ruido aceptado en el ledger.
- **Bandera:** `KERNEL_LEDGER_ENABLED` (entorno, global, encendida por defecto) es la bandera de la HU. Es
  observabilidad y no cambia la conducta ni el prompt (el gate de evals en replay pasa sin regrabar), así que no va
  detrás de `AOS_RUNTIME`/`AOS_PROCEDURES`, que quedan para cambios de conducta. Apagada, no hay filas y el `turn_id`
  queda NULL.
- **Kernel y orquestador:** `core/kernel/decisions.py` (ledger), `ingress.py` (cuota y presupuesto, desde el preámbulo
  del loop) y `egress.py` (un mensaje por turno, antes duplicado en consumer y debounce). El kernel no importa el loop,
  el consumer ni el debounce (contrato `kernel-sin-orquestador` en `.importlinter`). Las políticas del consumer
  (pausa de ADR-0122, kill-switch, palabras, horario, velocidad) registran su decisión en el sitio y se mudan a
  `ingress` cuando entre HU-AOS-11, que hoy edita ese bloque.

## Adenda de implementación (2026-10-10, HU-AOS-05): motor de procedimientos y modo sombra

- **Motor activo** con `tenants.aos_procedures`. Las convenciones del motor están en la adenda de
  ADR-0113 de la misma fecha.
- **Modo sombra** sin bandera nueva: la ventana es `aos_runtime = 'v3'` con `aos_procedures`
  apagada (el CHECK de 0070 ya ordena las dos).
  - El turno sigue con el flujo de siempre. El motor observa cada acción ejecutada sobre una
    tarea sombra en Redis (`aos:sombra:{negocio}:{conversación}`, TTL de un episodio).
  - La tarea sombra no guarda valores del cliente: solo nombres de datos llenos, la etapa y los
    booleanos de los `requiredWhen`.
  - No ejecuta precondiciones ni *resolvers*.
  - Registra las diferencias (códigos y conteos) en `audit_log` (`core:motor_sombra`,
    `diferencias`), una fila por lote de acciones.
  - Sin procedimientos registrados cuesta un chequeo en memoria. El replay del gate (runtime
    legacy) no la ejecuta.
- **Pendiente para HU-AOS-15:** el fail-closed de `core_decisions` con `aos_procedures`
  encendida. El motor no lo necesita: un `turn_id` NULL en `task_events` es válido.
