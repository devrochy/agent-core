# ADR-0112 — Agent OS de procedimientos: Coordinador por conexión, un hilo por conversación y agentes especialistas

- **Estado:** propuesto
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
