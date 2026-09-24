# ADR-0054 — Flujo de atención declarativo por pack, personalizable por tenant

- **Estado:** aceptado
- **Fecha:** 2026-09-23

## Contexto

Hoy el "flujo de venta" de `pack-commerce` vive como prosa en
`packages/pack-commerce/playbooks/ventas.md` y las tools se agrupan por skill
semántica (`LeadSkill`, `CatalogSkill`, `OrderSkill`) con selección por keywords
(`core/orchestrator/skill_router.py`). No existe una noción de **etapa de la
atención** (saludo, cotización, pedido) que agrupe las tools, ordene el prompt y
pueda personalizarse por tenant. En `agent-sale` ese flujo es explícito y
determinista; en agent-core está disperso entre playbook, skills y gates sueltos
(`confirmar_cotizacion`). Al habilitar un pack, el tenant no recibe una "base de
flujo de venta" reconocible ni una forma declarativa de agregar/quitar tools.

## Decisión

Se introduce el **flujo de atención**: una lista ordenada de **secciones** (cada una
con `id`, `label`, `purpose`, `pack` y `tools`). El flujo **no toca el contrato
congelado** (`@agentcore/contracts`); es un artefacto de pack + tenant.

1. **Default del pack:** `packages/pack-*/flow.json` (y `plugins/*/flow.json`),
   versionado con el pack, igual que sus `playbooks/`. Referencia tools **por
   nombre** (nunca importa a otro pack: invariante nº5).
2. **Copia del tenant:** `TENANTS_ROOT/<slug>/flow.json`, sembrada al habilitar el
   pack en el alta (`core/onboarding.py`), igual que `catalog.json`/`docs/`. Si el
   archivo existe, es **autoritativo** (permite quitar tools/secciones); si no,
   el runtime usa el default de los packs habilitados (fail-open).
3. **Resolución:** `core/agents/flow.py` valida (Pydantic/dataclasses), resuelve
   nombres contra las tools realmente disponibles (registradas + declarativas,
   ADR-0052) e ignora nombres desconocidos con warning, nunca tumba el turno.
4. **Consumo:** el flujo resuelto se inyecta como bloque estable del system prompt
   (`core/llm/prompt_builder.py`, antes de los bloques dinámicos) y define el
   conjunto de tools del turno (`core/orchestrator/loop.py`). Las secciones **no
   restringen** en runtime qué tool puede llamarse: `_tool_denial`
   (packs/bindings/scopes, ADR-0019/0050) sigue siendo la única compuerta de
   ejecución. El enforcement de secuencia sigue en gates deterministas
   (`confirmar_cotizacion`), no en una máquina de estados.
5. **Operación:** `agent-core flow show|validate|sync <slug>`. La edición es por
   archivo del tenant (ADR-0017/0026: sin autoservicio de instrucciones en el
   panel); el panel solo muestra el flujo resuelto en modo lectura.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Máquina de estados por etapa (estilo agent-sale `FORZAR_SIGUIENTE_TOOL`) | Complejidad alta y riesgo de bloquear conversaciones legítimas; el repo ya eligió gates deterministas sobre state machines (ver ROADMAP 2026-09-12). |
| Declarar el flujo en `AgentSpec.extensions["flow"]` | El usuario eligió archivo del tenant; además `extensions` es spec versionable y el flujo es operativo/editable por filesystem, como los playbooks (ADR-0026). |
| Flujo como un playbook más | Un `.md` no da estructura (secciones + tools) ni permite validar que las tools existan ni sembrar por pack. |
| Reusar `BaseSkill.keywords`/SkillRouter como "secciones" | El router es relevancia por turno, no estructura de negocio; no es personalizable por tenant ni ordena el prompt. |
| Solo en `pack-commerce` | Se reescribiría al generalizar; el costo de hacerlo genérico es marginal (un archivo por pack + un resolvedor). |

## Consecuencias

**Positivas:** al habilitar un pack el tenant recibe una base de flujo de venta
reconocible y editable; agregar/quitar tools por sección es configuración, no
código; el prompt gana una estructura estable y cacheable; cualquier pack puede
declarar su flujo; sin cambio de contrato.

**Negativas / lo que aceptamos pagar:** dos fuentes para "qué tools ve el
asistente" (el flujo y `AgentSpec.tools`), con precedencia explícita (el binding
`enabled=false` siempre deniega); el flujo del tenant no se actualiza solo cuando
el pack cambia (hay que correr `flow sync`); la edición es por archivo, no panel.

**Qué haría que revisáramos esto:** que un tenant necesite lógica de secuencia que
el flujo no exprese (→ máquina de estados o gates nuevos), que el flujo y
`AgentSpec.tools` diverjan en producción de forma recurrente (→ unificar en una
sola fuente), o que la copia del tenant se desincronice de los packs con
frecuencia (→ resolución por merge en vez de copia autoritativa).

## Impacto en el código

Nuevos: `packages/pack-commerce/flow.json`, `core/agents/flow.py`,
`cli/commands/flow.py`, `tests/test_flow.py`.
Modificados: `core/onboarding.py` (siembra), `core/llm/prompt_builder.py` (bloque
de flujo), `core/orchestrator/loop.py` (selección de tools), panel de
configuración (vista lectura), `docs/ASSISTANT-OS.md`, `docs/ROADMAP.md`.
Sin cambios en `packages/contracts`.
