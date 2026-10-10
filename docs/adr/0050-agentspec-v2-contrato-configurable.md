# ADR-0050 — AgentSpec v2.0.0: contrato que no miente y agente configurable

- **Estado:** aceptado
- **Fecha:** 2026-09-23

## Contexto

El contrato `AgentSpec` v1.0.0 (`packages/contracts/agentcore_contracts/contracts.py`,
ADR-0011) anticipó un agente empresarial —memoria, políticas, horarios, scopes de
tools— pero buena parte de esos campos **nunca se leen en runtime**: `temperature`,
`model.fallback/router`, todo `memory.*`, `policies.escalation.*`,
`confirmBeforeWrite` y `tools[].scopes/maxCallsPerConversation/requiresConfirmation`.
Un contrato con campos muertos es una promesa falsa: quien configura espera un efecto
que no ocurre. El inventario campo a campo está en `docs/AGENTSPEC-V2-INVENTARIO.md`.

## Decisión

Se publica **`AgentSpec` v2.0.0** y cada campo pasa a tener efecto real o desaparece:

1. **Persona**: `tone`, `warmth`, `emojis` se inyectan como variables en el prompt
   (`prompt_builder`); `forbiddenPhrases` se evalúa como guardrail post-LLM;
   `maxLength` limita la longitud del turno. Se **elimina `locale`** (el idioma vive
   en `AppSettings.language`).
2. **Model**: `temperature` se pasa al provider; `fallback` es un segundo modelo real
   si el primario falla; `router` define el modelo de razonamiento **por tenant**
   (hoy global).
3. **Memory**: `shortTermTurns`, `rag.topK` y `rag.minScore` reemplazan las
   constantes hardcodeadas de `working.py`/`vector.py`. Se **eliminan `summarize` y
   `rag.collections`** (no se implementan en esta entrega).
4. **Policies**: se mantienen `maxToolCallsPerTurn` y `budgetUsdPerConversation`. Se
   **elimina `escalation`** (la escalación operativa vive en
   `AppSettings.escalation_config`; horarios/SLA son Tanda 3) y `confirmBeforeWrite`.
5. **ToolBinding**: se implementa el **motor real** de `scopes`,
   `maxCallsPerConversation` y `requiresConfirmation` en la costura de ejecución de
   tools (`loop.py`). `enabled` ya funcionaba.
6. **Nuevo campo `extensions`** (JSON libre) para extensibilidad por tenant sin fork.
   En esta entrega se persiste y viaja en el spec; el motor de hooks que lo consume es
   Tanda 3 (alcance explícito).
7. **Una fuente por campo**: lo **versionable** (persona, modelo, memoria, tools,
   extensions) vive en el spec; lo **operativo** (kill-switch, BYOK, umbrales de
   routing, keywords de escalamiento) en `AppSettings`. Se deprecia
   `behavior_config["tono"]`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dejar v1.0.0 y agregar campos nuevos | No arregla la mentira: siguen los muertos; el contrato crece sin control. |
| Solo eliminar los muertos, sin implementar nada | Pierde capacidades valiosas (temperatura, memoria configurable) que ya estaban pensadas. |
| Implementar todo incluido horarios/SLA/`summarize` | Scope creep: horarios/SLA son operación (Tanda 3) y `summarize` no tiene caso de uso hoy. |
| Contrato nuevo en paralelo (v3) sin tocar v1 | Duplicación y dos fuentes; nadie migra. |

## Consecuencias

**Positivas:** el contrato deja de prometer en vano; el agente es configurable en
modelo, memoria, personas y tools con efecto real; la separación versionable/operativo
elimina duplicados; `extensions` abre la puerta a personalización sin fork.

**Negativas / lo que aceptamos pagar:** **rompe la forma congelada** (bump major
v1.0.0 → v2.0.0) y exige `load_active_spec` tolerante (specs v1 guardados siguen
cargando: los campos eliminados se ignoran y los nuevos toman default); hay que tocar
runtime sensible (`loop.py`, providers, memoria); `requiresConfirmation` introduce un
estado de "espera de confirmación" en el loop que agrega complejidad.

**Qué haría que revisáramos esto:** que `extensions` se use sin motor que lo consuma
(campo muerto otra vez), o que el motor de `scopes` no sea suficiente para las
necesidades de autorización por tool y haga falta un RBAC de tools completo.

## Impacto en el código

`packages/contracts/agentcore_contracts/contracts.py` + `__init__.py` (v2.0.0),
`core/agents/spec.py` (`DEFAULT_SPEC`/`ensure_default_spec` tolerante),
`core/llm/prompt_builder.py` y `core/llm/base.py`+providers (temperature/persona),
`core/llm/__init__.py`/`core/orchestrator/loop.py` (fallback/router/scopes/maxCalls/
requiresConfirmation), `core/memory/working.py` y `vector.py` (memoria/rag),
`core/orchestrator/guardrails.py` (forbiddenPhrases), `core/admin/templates/spec.html`
y pestañas. Fixtures/tests: `tests/test_agent_spec.py` y los que construyen `AgentSpec`.
Ver `docs/AGENTSPEC-V2-INVENTARIO.md`.

## Coherencia de la navegación (misma tanda)

El editor `/admin/spec` se movió a la pestaña **"Avanzado"** de Configuración
(historial de versiones + `raw_json`). `/admin/spec` se conserva como ruta que
redirige al tab (compatibilidad con enlaces/docs); la sección RBAC `spec` se
absorbe en `settings`. La pestaña **Identidad** gana los campos de persona que solo
estaban en `raw_json` (`role`/`warmth`/`emojis`/`forbiddenPhrases`/`maxLength`).

La barra de pestañas pasa a ser un **partial compartido del core**
(`_config_tabs.html`) alimentado por `config_tabs()` (fuente única): antes la
pestaña "Pagos" (del pack) dibujaba su propia barra con solo dos pestañas, así que
al abrirla "desaparecían" las del core. Ahora el pack no hardcodea la barra.

## Correcciones post-auditoría (`auditor-multitenant`)

- **Fallback real**: `_resolved_provider_fallback` ahora usa `resolve_provider` (el
  clon por `__dict__` fallaba siempre y reintentaba el primario).
- **`maxCallsPerConversation`**: cuenta solo `action="execute"` (denegaciones y
  pedidos de confirmación ya no consumen el cupo).
- **`requiresConfirmation`**: limpia `pending_confirmation` tras ejecutar y actualiza
  el estado en memoria para que la repetición dentro del mismo turno funcione.
- **Habilidades (POST)**: filtra por `enabled_packs` (no pisa bindings de packs
  deshabilitados ni persiste tools ajenas).
- **`scopes`**: enforcement real — si la tool declara scopes y el binding no los
  concede, se deniega.
- **Escalación v1**: `_migrate_legacy_escalation` copia `policies.escalation.triggers`
  a `AppSettings.escalation_config` antes de depurar (sin pérdida silenciosa).
- **Bajos**: clamp de `temperature`/`maxToolCallsPerTurn`, `rag.minScore` invertido a
  distancia (label correcto), `_persona_guard` sin interpolar la frase,
  `_load_history` con `tenant_id` explícito, clave Redis con namespace de tenant.

## Adenda (2026-10-10): `persona.maxLength` es un tope blando

Pendiente de aprobación del dueño (PR a `develop`). La revisó el agente `arquitecto` antes de
escribir código. La forma del contrato no cambia (`maxLength: int`); cambia solo el efecto en runtime.

**Caso.** En los journeys del asistente de SolversHub (HU-SH-08, `evals/journeys.py --tenant solvershub 01
--k 4`, 2026-10-09), "¿qué planes tienen y cuánto cuestan?" escaló 1 de 4 veces: el modelo listó los 4
planes, pasó de 500 caracteres dos veces seguidas y el turno escaló y pausó el bot. Un exceso de largo no es
una afirmación falsa, y escalar una consulta válida pierde ventas.

**Decisión.**
- `persona.maxLength` es una guía de estilo, no un guardrail. Nunca escala, nunca pausa el bot y nunca
  responde neutral (`verifying`). `0` lo apaga, y el negocio puede subir su valor.
- Se mide sobre el borrador que escribió el modelo (después de `strip_markdown` y de quitar links sin
  respaldo), sin los `_safe_append` del servidor.
- Se evalúa **al final**: después de `forbiddenPhrases`, del verificador (ADR-0119) y de los guardrails de
  packs. Un borrador que falla algo de eso sigue el camino de siempre (`guardrail_max_retries`,
  `guardrail_fail_action`).
- Tiene **un** reintento propio, fijo, que no toca `guardrail_retries` ni lee `guardrail_fail_action`: esos
  son de veracidad (ADR-0092, ADR-0113 §4).
- **El reintento de largo nunca empeora el resultado.** El borrador largo ya verificado se guarda. Si el texto
  acortado falla una verificación dura (p. ej. redondea o inventa un precio), se entrega el borrador largo.
  Si pasa, se entrega el acortado, siga largo o no.
- Solo se reintenta si queda una vuelta del bucle (`maxToolCallsPerTurn`) y presupuesto
  (`budgetUsdPerConversation`). Si no, el reintento terminaría en `max_iterations` (escala) o en el cierre
  por presupuesto, y la respuesta se entrega sin reintento.
- La instrucción del reintento es la sección `persona_max_length` del playbook `respuestas` (invariante 3),
  con `{max_length}` reemplazado por el entero del spec. En código queda solo el texto de respaldo, igual al
  que había. La sección se agrega con la próxima regrabación de las evals: `respuestas` entra en el system
  prompt de todos los negocios y cambiarlo invalida todas las grabaciones (adenda de ADR-0119).
- **Rastro:** cada respuesta entregada por encima del tope deja una fila en `audit_log`
  (`actor = core:persona`, `action = persona_max_length_excedido`) con `largo`, `tope` y `entregado`
  (`reintento`, `guardado` o `sin_reintento`), sin texto. Es un dato por turno: HU-AOS-10 lo mueve a
  `turn_outcomes` cambiando solo `_registrar_largo_excedido`, como `registrar_veredicto`.
- El envío ya parte la respuesta en burbujas según `split_style`. Como ahora puede llegar con miles de
  caracteres, ninguna burbuja pasa el límite técnico de WhatsApp (4096): también se parten la cola que
  junta el tope de burbujas y un párrafo enorme.
- La regla declarativa `max_length` de `extensions["guardrails"]` (ADR-0051 §3) **no cambia**: es el tope
  duro que un negocio pide de forma explícita, con su `action`.

**Descartado.** Recortar por oraciones (pierde justo lo que el cliente pidió, p. ej. el precio del cuarto
plan, y puede dejar una afirmación a medias). Subir el default de 500 (el número sirve para WhatsApp: el
problema era la consecuencia). Hacerlo configurable por negocio (YAGNI: `0` y el valor ya lo controlan).

**Qué haría que revisáramos esto:** que `persona_max_length_excedido` aparezca en más del 10 % de los turnos
de un negocio (su tope no le sirve) o quejas de mensajes demasiado largos.

**Código:** `core/orchestrator/loop.py` (`_persona_frase_prohibida`, `_persona_excede_largo`,
`_instruccion_largo`, `_registrar_largo_excedido`; se retira `_persona_guard`),
`core/gateway/message_splitter.py`. Tests: `tests/integration/test_persona_largo_loop.py`,
`tests/test_agent_spec.py`, `tests/test_message_splitter.py`.
