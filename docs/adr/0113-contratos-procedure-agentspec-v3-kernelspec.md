# ADR-0113 — Contratos `procedure/1`, AgentSpec v3 y KernelSpec v1 (major)

- **Estado:** aceptado (2026-10-09, HU-AOS-01)
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-03 y HU-AOS-12 (SPEC-26). Enmienda ADR-0050 (AgentSpec v2), ADR-0014 (la allowlist de
  playbooks pasa a ser la de cada agente), ADR-0051 §3 y ADR-0052 (guardrails y tools declarativas salen de
  `extensions`), ADR-0054 y ADR-0109 (flujo de atención). Usa ADR-0119 (verificador y `register_afirmaciones`),
  ADR-0118 (instalaciones con scope) y ADR-0077 (tipos del panel desde el OpenAPI).

## Contexto

- Los contratos solo cambian con un ADR (invariante 4, ADR-0011). El paquete Python `agentcore-contracts` está en v2.0.0 (ADR-0050); el espejo TS de `packages/contracts/src/` quedó en 1.0.0 y nadie lo consume (los tipos del panel salen del OpenAPI, ADR-0077).
- El AgentSpec describe **un** asistente por negocio. `extensions` lleva hoy las reglas declarativas de guardrails (ADR-0051, `core/orchestrator/loop.py`) y las tools declarativas del negocio (ADR-0052, `core/mcp/declarative.py`), estas con `headers` literales que pueden traer secretos, aunque el comentario del contrato aún lo marca como reservado.
- El flujo de atención es un artefacto de packs y negocio que se inyecta como texto del prompt. Sus secciones no restringen qué acción se llama.
- Un playbook sin validar tumbó el primer turno de los negocios (core #305).

## Decisión

Los contratos pasan a v3.0.0 con tres modelos nuevos: `procedure/1`, AgentSpec v3 y KernelSpec v1.

1. **`procedure/1`** (lo publica la habilidad) define:
   - disparadores: descripción, ejemplos y prefijos de botones;
   - datos: tipo, *resolver* del servidor, obligatoriedad, confirmación, clase de dato y `setBy` (una acción o un payload interactivo, como el `plan:` de comercio o las listas de agenda), condición por tipo de contenido (ubicación, imagen) y candidatos para elegir; el motor los llena sin pasar por el LLM;
   - etapas: objetivo, **acciones permitidas**, condición de salida, confirmación y riesgo;
   - precondiciones, cierre por eventos de dominio y recuperación;
   - afirmaciones que el verificador comprueba y escenarios de prueba. Las afirmaciones se compilan a `Afirmacion` y se registran por `register_afirmaciones` (ADR-0119): hay un solo verificador y una sola API. Mientras un pack no publique procedimientos, sigue registrándolas como hoy.

   Un **linter** exige acciones existentes, etapas alcanzables, confirmación para toda acción `irreversible` y fuente para toda afirmación de dinero. El mismo validador corre en el CLI, en CI y en la API al guardar un borrador o publicar una release: un procedimiento, AgentSpec o KernelSpec inválido no se guarda (el incidente de #305 salió de un playbook de un negocio, no de un pack).

   Un procedimiento solo usa acciones de su habilidad y del core. Lo de otra habilidad llega por eventos de dominio (ADR-0023, ADR-0107) o por registros del core (ADR-0067), nunca importándola (invariante 5). Los *resolvers* reciben solo `ToolContext` (invariante 6).
2. **AgentSpec v3** (un agente):
   - `description`. `kind` vive en `agents`, no en el contrato: un agente `core` versiona un KernelSpec v1 y un AgentSpec v3 solo describe agentes `specialist` o `backoffice`. La identidad (`id`, `tenantId`, versión) no está en el contrato: la da la fila;
   - `persona` (`inheritConnectionVoice`, `maxLength`, `forbiddenPhrases`), `model` (los mismos campos que en v2: `primary`, `fallback`, `router`, `temperature`) y `playbooks`;
   - `procedures` (id, rango de versión y overrides de frases y textos);
   - `exposes` (procedimientos y consultas tipadas para otros agentes);
   - `memory` (`readScopes`, `writeNamespaces`);
   - `tools` (bindings: consentimiento, límites y acciones fuera de etapa), `guardrails` y `declarativeTools`;
   - `policies` (`maxToolCallsPerTurn`).

   `declarativeTools` **no contiene secretos**: un header sensible se declara por referencia (`{"Authorization": {"secret": "<nombre>"}}`) y su valor vive cifrado en la configuración de la instalación (`config_encrypted`, ADR-0118 §5). El linter y el validador al guardar rechazan valores literales en `Authorization`, `Cookie` y `X-Api-Key`. El lector tolerante mueve los secretos literales de v2 a la configuración cifrada antes de crear la primera `agent_version`, que es inmutable y entra en cada release (ADR-0117).
3. **KernelSpec v1** (el Coordinador):
   - `voice` de la conexión (rol, tono, calidez, emojis) y `episode` (`idleMinutes`, ADR-0117);
   - enrutamiento: agente por defecto, reglas en un DSL **sin regex** (`interactivePayloadPrefix`, `keywordAny`, `customerFact`, `flowStage`, `channel`, `withinHours`), umbrales de disparadores, coprocesador acotado y estabilidad del dueño;
   - procedimientos (`maxStack`, confirmación de lo irreversible), `modelPolicy` (solo lo que cambia en etapas de riesgo alto), límites de colaboración del turno, composición en un mensaje y playbook de respuestas;
   - `policies.budgetUsdPerConversation`: la conversación es de la conexión y tiene N agentes, así que el tope es uno solo;
   - `policies.backofficeBudgetUsdMonthly`: tope mensual del back-office de la conexión (ADR-0116 §5), que solo baja `costo_llm_usd` del plan.
4. **Una sola fuente por campo:**
   - límites de colaboración del turno: KernelSpec; los de cada enlace: `agent_links` (ADR-0116), que solo puede bajarlos;
   - modelo base y de respaldo: el agente;
   - reintentos y acción ante un fallo del verificador: la configuración del negocio de ADR-0092;
   - presupuesto por conversación: KernelSpec;
   - confirmación de una acción: la etapa del procedimiento. El `requiresConfirmation` del binding solo aplica a acciones fuera de etapa. El KernelSpec solo puede exigir confirmación además de esas, nunca quitarla; lo `irreversible` siempre se confirma;
   - voz: la `voice` del KernelSpec manda para rol, tono, calidez y emojis. `AppSettings.brand_voice` sigue siendo dato del negocio y se edita en el panel, pero la release congela su valor (ADR-0117 §2): un cambio entra con la siguiente release. `behavior_config.tono` deja de escribirse y se lee solo como respaldo de v2;
   - packs habilitados: `pack_installations`, leídas solo por `enabled_packs()`, `enabled_business_packs()` y `count_tenant_packs()` (semántica en ADR-0118 §2);
   - textos de un procedimiento: se sobrescriben solo en `procedures[].overrides`; los playbooks, solo en `TENANTS_ROOT` (ADR-0026). La release congela ambos (ADR-0117).
5. **Lector tolerante v2 → v3:** el AgentSpec v2 activo de un negocio se carga como el agente **General** de **cada** conexión conversacional del negocio. Hasta que se construya su release 1 (ADR-0117 §2), el KernelSpec se deriva en memoria de `persona` v2 (rol, tono, calidez, emojis) y de los valores por defecto. Los presets (ADR-0110) y el alta (ADR-0121) siguen escribiendo un AgentSpec v2 que el lector tolerante convierte, hasta HU-AOS-46 (ADR propio). Cada sección del flujo compuesto pasa a un procedimiento "abierto" (una etapa con sus acciones), y todas las acciones que el flujo v2 ofrecía quedan en sus `tools`: nada cambia de conducta aunque la etapa en foco sea otra. La correspondencia, campo por campo:

| Campo v2 | En v3 |
|---|---|
| `id`, `version`, `tenantId` | Identidad y versión del agente: `agents` y `agent_versions` (`key`, número y hash) |
| `persona.role`, `tone`, `warmth`, `emojis` | **Voz de la conexión**: `voice` del KernelSpec, versionada con la release; el agente la hereda |
| `persona.maxLength`, `forbiddenPhrases` | `persona` del agente |
| `model` | `model` del agente; el KernelSpec solo dice qué cambia en etapas de riesgo alto |
| `memory` | `memory`, más `readScopes` y `writeNamespaces` |
| `playbooks` | `playbooks` del agente: la allowlist de ADR-0014 pasa a ser por agente |
| `tools` (bindings) | `tools`: consentimiento, límites y acciones fuera de etapa (fail-closed, ADR-0118) |
| `policies` | `maxToolCallsPerTurn` en `policies` del agente; `budgetUsdPerConversation` en el KernelSpec |
| `packs` | La habilitación pasa a `pack_installations` con scope (ADR-0118), leída por `enabled_packs`. Sale del contrato cuando ningún lector lea el campo |
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

- `packages/contracts`: `agentcore-contracts` (Python) a v3.0.0; el espejo TS de `packages/contracts/src/` se retira y se corrige `test_version_congelada`.
- `core/agents/spec.py` (lector tolerante, `enabled_packs`) y `core/agents/flow.py` (conversión a procedimientos).
- `core/orchestrator/loop.py` y `core/mcp/declarative.py`: leen `guardrails` y `declarativeTools` en vez de `extensions`; los headers sensibles se resuelven desde la configuración cifrada.
- `core/llm/prompt_builder.py` (lee `playbooks` y `persona`).
- Todos los call-sites de `enabled_packs`, `enabled_business_packs`, `count_tenant_packs` y `spec.packs` en `core/`, `cli/` y `packages/pack-*` (ADR-0118 §2). El CI falla si queda una lectura de `spec.packs` fuera del lector tolerante.
- Editores: `core/admin/advanced_service.py` y `core/api/routers/settings.py` (identidad, modelo, guardrails), más `docs/openapi.json` y `panel-web`. Las vistas SSR de `core/admin/views.py` que editan `extensions` no se migran: se retiran. El editor de tools declarativas hoy solo existe en SSR, así que se crea su ruta en `/api/v1`.
- `packages/pack-*/`: procedimientos en el manifiesto.
- Linter y validador en `cli/`, en CI y en la API.
