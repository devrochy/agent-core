# ADR-0113 — Contratos `procedure/1`, AgentSpec v3 y KernelSpec v1 (major)

- **Estado:** propuesto
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-03 y HU-AOS-12 (SPEC-26). Enmienda ADR-0050 (AgentSpec v2), ADR-0014 (la allowlist de
  playbooks pasa a ser la de cada agente), ADR-0051 §3 y ADR-0052 (guardrails y tools declarativas salen de
  `extensions`), ADR-0054 y ADR-0109 (flujo de atención).

## Contexto

- `@agentcore/contracts` solo cambia con un ADR (invariante 4, ADR-0011) y está en v2.0.0 (ADR-0050).
- El AgentSpec describe **un** asistente por negocio. `extensions` lleva hoy las reglas declarativas de guardrails (ADR-0051, `core/orchestrator/loop.py:513-514`) y las tools declarativas del negocio (ADR-0052, `core/mcp/declarative.py:127`), aunque el comentario del contrato aún lo marca como reservado.
- El flujo de atención es un artefacto de packs y negocio que se inyecta como texto del prompt. Sus secciones no restringen qué acción se llama.
- Un playbook sin validar tumbó el primer turno de los negocios (core #305).

## Decisión

Los contratos pasan a v3.0.0 con tres modelos nuevos: `procedure/1`, AgentSpec v3 y KernelSpec v1.

1. **`procedure/1`** (lo publica la habilidad) define:
   - disparadores: descripción, ejemplos y prefijos de botones;
   - datos: tipo, *resolver* del servidor, obligatoriedad, confirmación, clase de dato y `setBy` de una acción;
   - etapas: objetivo, **acciones permitidas**, condición de salida, confirmación y riesgo;
   - precondiciones, cierre por eventos de dominio y recuperación;
   - afirmaciones que el verificador comprueba y escenarios de prueba.

   Un **linter** en el CLI y en CI exige acciones existentes, etapas alcanzables, confirmación para toda acción `irreversible` y fuente para toda afirmación de dinero.

   Un procedimiento solo usa acciones de su habilidad y del core. Lo de otra habilidad llega por eventos de dominio (ADR-0023, ADR-0107) o por registros del core (ADR-0067), nunca importándola (invariante 5). Los *resolvers* reciben solo `ToolContext` (invariante 6).
2. **AgentSpec v3** (un agente):
   - `kind` (`core`, `specialist` o `backoffice`) y `description`;
   - `persona` (`inheritConnectionVoice`, `maxLength`, `forbiddenPhrases`), `model` (primario y respaldo) y `playbooks`;
   - `procedures` (id, rango de versión y overrides de frases y textos);
   - `exposes` (procedimientos y consultas tipadas para otros agentes);
   - `memory` (`readScopes`, `writeNamespaces`);
   - `tools` (bindings: consentimiento, límites y acciones fuera de etapa), `guardrails` y `declarativeTools`;
   - `policies` (`maxToolCallsPerTurn`, `budgetUsdPerConversation`).
3. **KernelSpec v1** (el Coordinador):
   - `voice` de la conexión (rol, tono, calidez, emojis) y `episode` (`idleMinutes`, ADR-0117);
   - enrutamiento: agente por defecto, reglas en un DSL **sin regex** (`interactivePayloadPrefix`, `keywordAny`, `customerFact`, `flowStage`, `channel`, `withinHours`), umbrales de disparadores, coprocesador acotado y estabilidad del dueño;
   - procedimientos (`maxStack`, confirmación de lo irreversible), `modelPolicy` (solo lo que cambia en etapas de riesgo alto), límites de colaboración del turno, composición en un mensaje y playbook de respuestas.
4. **Una sola fuente por campo:**
   - límites de colaboración del turno: KernelSpec; los de cada enlace: `agent_links` (ADR-0116), que solo puede bajarlos;
   - modelo base y de respaldo: el agente;
   - reintentos y acción ante un fallo del verificador: la configuración del negocio de ADR-0092.
5. **Lector tolerante v2 → v3:** el AgentSpec v2 activo de un negocio se carga como el agente **General**. Cada sección del flujo compuesto pasa a un procedimiento "abierto" (una etapa con sus acciones), y todas las acciones que el flujo v2 ofrecía quedan en sus `tools`: nada cambia de conducta aunque la etapa en foco sea otra. La correspondencia, campo por campo:

| Campo v2 | En v3 |
|---|---|
| `id`, `version`, `tenantId` | Identidad y versión del agente: `agents` y `agent_versions` (`key`, número y hash) |
| `persona.role`, `tone`, `warmth`, `emojis` | **Voz de la conexión**: `voice` del KernelSpec, versionada con la release; el agente la hereda |
| `persona.maxLength`, `forbiddenPhrases` | `persona` del agente |
| `model` | `model` del agente; el KernelSpec solo dice qué cambia en etapas de riesgo alto |
| `memory` | `memory`, más `readScopes` y `writeNamespaces` |
| `playbooks` | `playbooks` del agente: la allowlist de ADR-0014 pasa a ser por agente |
| `tools` (bindings) | `tools`: consentimiento, límites y acciones fuera de etapa (fail-closed, ADR-0118) |
| `policies` | `policies` |
| `packs` | Sale del contrato: la habilitación pasa a `pack_installations` con scope (ADR-0118) |
| `extensions.guardrails` (ADR-0051) | `guardrails` de primer nivel |
| `extensions.tools` (ADR-0052) | `declarativeTools` de primer nivel |
| `extensions` | Sigue reservado y sin semántica ("el contrato no miente", ADR-0050) |

Los textos del clasificador y del compositor viven en playbooks del core (invariante 3). Descripciones, ejemplos y frases son datos de la habilidad o del negocio, no código.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Meter la semántica nueva en `extensions` | Viola "el contrato no miente" (ADR-0050) y `extensions` ya mezcla dos usos |
| JSON libre por agente | Sin validación: repite el incidente de #305 |
| Mantener el flujo como guía del prompt | No garantiza el orden ni las acciones de cada etapa |

## Consecuencias

**Positivas:** recorridos verificables antes de llegar a producción; acciones fail-closed por etapa; agentes versionados y reutilizables entre conexiones; cada campo con una sola fuente.

**Negativas / lo que aceptamos pagar:** un major de contrato; migrar en el mismo PR las habilidades, el plugin del negocio y los editores del panel de guardrails y tools declarativas; mantener el lector tolerante hasta retirar v2.

**Qué haría que revisáramos esto:** que el contrato de procedimientos no exprese un caso real de las verticales (comercio, agenda, suscripciones, cobranza) sin recurrir a código.

## Impacto en el código

- `packages/contracts` (v3.0.0).
- `core/agents/spec.py` (lector tolerante) y `core/agents/flow.py` (conversión a procedimientos).
- `core/orchestrator/loop.py` y `core/mcp/declarative.py`: leen `guardrails` y `declarativeTools` en vez de `extensions`.
- `core/admin/`: editores de guardrails y tools declarativas.
- `packages/pack-*/`: procedimientos en el manifiesto.
- Linter en `cli/` y en CI.
