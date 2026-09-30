# AgentSpec v2 — Inventario campo a campo para la Tanda 2

> **Estado: DECIDIDO e IMPLEMENTADO (ADR-0050).** Este documento queda como registro
> del análisis; las decisiones se aplicaron al contrato v2.0.0. Ver
> `docs/adr/0050-agentspec-v2-contrato-configurable.md`.
>
> Documento de trabajo para decidir qué hacer con cada campo del contrato `AgentSpec`
> (opción B aprobada: cambiar el contrato). El contrato era congelado v1.0.0
> (`packages/contracts/agentcore_contracts/contracts.py`, ADR-0011): el cambio de
> forma se hizo con ADR-0050 y **bump major (v2.0.0)**.

## Criterio

Por cada campo se marca una de tres acciones:

- **implementar** → el campo existe y debe tener efecto real en runtime (indicando dónde se leería).
- **eliminar** → se quita del contrato (no tiene uso y no se va a implementar).
- **reservar** → se mantiene pero se documenta como "reservado para el futuro" (sin efecto hoy).

Regla de oro: **un campo que no se lee en runtime es código muerto o mentira del
contrato**. La decisión por campo es `implementar` o `eliminar`; `reservar` solo si
hay plan concreto.

---

## 1. `Persona`

| Campo | Tipo | Estado runtime hoy | Recomendación | Dónde se leería si se implementa |
|---|---|---|---|---|
| `role` | str | Se guarda; no se usa en el prompt (los playbooks tienen su propia identidad) | **implementar** | `prompt_builder.py` como placeholder `{{ role }}` en el bloque de identidad |
| `tone` | enum | **Muerto**: `loop.py:235` lo pasa a `PromptContext.tone` pero `build_system_blocks` no lo usa | **implementar** | `prompt_builder.py` (inyectar en el bloque de tono de un playbook o variable de plantilla) |
| `warmth` | 0-5 | Muerto | **implementar** | `prompt_builder.py` → variable de plantilla (o mapear a tono) |
| `emojis` | none/sparse/free | Muerto | **implementar** | `prompt_builder.py` → variable de plantilla |
| `maxLength` | int | Muerto | **eliminar** (o implementar como tope real del turno) | `loop.py` antes de enviar la respuesta (truncar/avisar) |
| `forbiddenPhrases` | list[str] | Muerto | **implementar** (guardrail genérico) | `core/orchestrator/guardrails.py` como guardrail "frases prohibidas" evaluado post-LLM |
| `locale` | str | Muerto | **implementar** (puede alimentar `language` de AppSettings) o **eliminar** | `prompt_builder.py` / resolver idioma del tenant |

## 2. `AgentModel`

| Campo | Estado runtime hoy | Recomendación | Dónde se leería |
|---|---|---|---|
| `primary` | **Sí** (`loop.py:610`) | mantener | ya se usa |
| `fallback` | Muerto | **implementar** (fallback real si el provider primario falla) o **eliminar** | `core/llm/__init__.py::resolve_provider` / `loop.py` (catch → segundo modelo) |
| `router` | Muerto | **implementar** (definir el modelo de razonamiento) o **eliminar** | `core/llm/difficulty_router.py` (el "reasoning" por tenant, hoy global) |
| `temperature` | Muerto (`BaseLLMProvider.generate` no lo recibe) | **implementar** | `core/llm/base.py` + providers (pasarlo en `generate`) |

## 3. `AgentMemory`

| Campo | Estado runtime hoy | Recomendación | Dónde se leería |
|---|---|---|---|
| `shortTermTurns` | Muerto (`working.py` hardcodea `MAX_TURNS=30`) | **implementar** | `core/memory/working.py` (reemplaza la constante) |
| `summarize` | Muerto | **implementar** (resumen de conversación larga) o **eliminar** | `loop.py` / `working.py` (cuando se superan los turnos) |
| `rag` (`RagConfig`) | Muerto (`vector.py` usa `top_k=3`, `max_distance=0.6` fijos) | **implementar** | `core/memory/vector.py` (`recall`: `topK`, `minScore`) |
| — `collections` | Muerto | **implementar** (colecciones por tenant) o **eliminar** | `vector.py` (scope de recall) |
| — `topK` | Muerto | **implementar** | `vector.py` |
| — `minScore` | Muerto | **implementar** | `vector.py` (reemplaza `max_distance`) |

## 4. `AgentPolicies`

| Campo | Estado runtime hoy | Recomendación | Dónde se leería |
|---|---|---|---|
| `maxToolCallsPerTurn` | **Sí** (`loop.py:323`) | mantener | ya se usa |
| `budgetUsdPerConversation` | **Sí** (`loop.py:310`) | mantener | ya se usa |
| `escalation.triggers` | **Muerto** (se usa `AppSettings.escalation_config["keywords"]`) | **eliminar** del contrato y quedarse con `AppSettings` (o unificar: ver §7) | — |
| `escalation.autoEscalateAfterTurns` | Muerto | **implementar** (autoescalado por turnos) o **eliminar** | `core/orchestrator/escalation.py` |
| `escalation.autoEscalateOnLowConfidence` | Muerto | **implementar** (usar el score del difficulty router) o **eliminar** | `escalation.py` |
| `escalation.businessHours` / `outOfHoursMessage` | Muerto | **implementar** (horarios por tenant) | `escalation.py` / `consumer.py` |
| `confirmBeforeWrite` | Muerto | **eliminar** (no hay confirmación de tools hoy y no es prioritario) o **implementar** | `loop.py` (pausa antes de tool write) |

## 5. `ToolBinding`

| Campo | Estado runtime hoy | Recomendación | Dónde se leería |
|---|---|---|---|
| `tool` | **Sí** (se usa) | mantener | `loop.py` `_tool_denial` |
| `enabled` | **Sí** (`loop.py:506-508`) | mantener | ya se usa (falta UI, Tanda 2) |
| `scopes` | Muerto (ningún pack lo setea) | **implementar** (motor de scopes por tenant) o **eliminar** | `loop.py` `_tool_denial` + contrato de tools |
| `maxCallsPerConversation` | Muerto | **implementar** (límite por conversación) o **eliminar** | `loop.py` (contador por conversación) |
| `requiresConfirmation` | Muerto | **eliminar** (o diferir) | — |

## 6. `PlaybookRef` y `packs`

| Campo | Estado runtime hoy | Recomendación |
|---|---|---|
| `playbooks[].name` / `description` | **Sí** (allowlist de `load_playbook`) | mantener; **agregar UI** para editar el índice (hoy solo `raw_json`) |
| `packs` | **Sí** (`enabled_packs`) | mantener (ya se gestiona en `/admin/plugins`) |

## 7. Duplicados entre `AgentSpec` y `AppSettings`

| Campo | Dónde está | Estado | Decisión propuesta |
|---|---|---|---|
| `tono` | `behavior_config["tono"]` (AppSettings) y `persona.tone` (spec) | Ambos **muertos** en runtime | Una sola fuente: `persona.tone` (spec, versionada); deprecar `behavior_config["tono"]` |
| Escalamiento | `escalation_config["keywords"]` (AppSettings, usado) y `policies.escalation.triggers` (spec, muerto) | Doble | Mantener `AppSettings.escalation_config` para keywords (operativo), quitar `policies.escalation` del contrato si no se implementa horarios/SLA en esta tanda |
| Difficulty thresholds | `behavior_config` (AppSettings, usado) | Único | Mantener en AppSettings (operativo, no versionar) |
| LLM BYOK | `AppSettings.llm_*` | Único | Mantener en AppSettings (operativo); `model.*` del spec para lo versionado |

**Principio propuesto**: lo **versionable** (comportamiento, persona, memoria, tools) va al
`AgentSpec`; lo **operativo** (kill-switch, BYOK, umbrales de routing, escalamiento por
keywords) va a `AppSettings`. Un campo, una fuente.

## 8. Campos nuevos propuestos para v2.0.0 (requieren ADR)

| Campo propuesto | Para qué | Nota |
|---|---|---|
| `extensions` (bloque `custom` JSON) | Extensibilidad sin fork: datos por tenant que un motor de hooks/guardrails declarativos lee | Cumple la invariante #2 ("config → pack → plugin") |
| `toolBindings` por **scopes** explícitos | Autorización fina por tool (reemplaza el `scopes` muerto por uno real) | Si se implementa `scopes` |

## 9. Metadatos del spec (`id`, `version`, `tenantId`)

- Se usan (versionado, `AgentSpecRecord`). **Mantener**; `tenantId` debería venir del
  tenant activo en vez de persistirse redundante (decisión menor).

---

## Resumen de decisiones que necesito confirmar

1. **`temperature`, `fallback`, `router`**: ¿los implementamos (pasar temperatura al provider, fallback real, reasoning por tenant) o eliminamos los que no alcancemos?
2. **`memory.*`**: ¿implementamos `shortTermTurns`/`rag.topK`/`minScore` (bajo costo, reemplazan constantes) y `summarize`/`collections` quedan fuera?
3. **`persona`**: ¿`tone`/`warmth`/`emojis` a variables de plantilla, `forbiddenPhrases` como guardrail genérico, y `maxLength`/`locale` se eliminan o se implementan?
4. **`policies.escalation`**: ¿lo eliminamos del contrato (se queda en `AppSettings.escalation_config`) o lo implementamos completo (horarios/SLA/autoescalado, es la Tanda 3)?
5. **`ToolBinding.scopes/maxCalls/requiresConfirmation`**: ¿motor real o eliminar?
6. **`extensions`**: ¿se agrega el bloque `custom` en v2.0.0?
7. **Duplicados**: ¿confirmás el principio "versionable → spec, operativo → AppSettings"?

Con estas respuestas se escribe el **ADR-0050 (contrato v2.0.0)** y se ejecuta la Tanda 2.