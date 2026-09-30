# ADR-0075 — Playground "Probar asistente": conversación de prueba sin canal

- **Estado:** aceptado
- **Fecha:** 2026-09-27

## Contexto

Hace falta poder probar el asistente de un tenant **sin conectar ningún canal**: para
demos (mostrar el agente antes de tener WhatsApp listo) y para validar cambios de
AgentSpec/playbooks sin ensuciar la operación real. El loop del runtime ya separa
generar de enviar: `process_conversation(conversation_id, latest_message) -> TurnResult`
(`core/orchestrator/loop.py`); el envío por canal lo hace el caller
(`consumer.py` → `send_to_conversation`). El panel ya tiene SSR (FastAPI + Jinja2).

## Decisión

Se agrega una vista **"Probar asistente"** que llama al **mismo loop** sobre una
conversación sintética y devuelve la respuesta al navegador, **sin pasar por ningún
canal** (`send_to_conversation` nunca se invoca).

1. **Conversación de prueba con `channel == "test"`.** Un `Customer` y una
   `Conversation` sintéticos por tenant (`core/admin/playground.py`), reusando el loop
   real (tools, guardrails, memoria) para máxima fidelidad.
2. **Historial en working memory, sin filas en `messages`.** El hilo de la prueba vive
   en Redis como cualquier conversación; NO se persisten `Message`, así el playground no
   consume la métrica `mensajes` ni aparece en las listas del negocio.
3. **Aislamiento de cuotas y escalamiento.** El canal `test` queda excluido de las
   métricas de billing (`clientes`, `conversaciones`, `costo_llm_usd`) y `escalate()`
   lo ignora (no crea `Escalation`, no pausa, no alerta). Listados del panel
   (conversaciones, dashboard) filtran el canal `test`.
4. **UI:** botón destacado en Conexiones (acento propio indigo), entrada en el menú, y
   vista tipo celular con aviso **"Modo prueba — no pasa por ningún canal"**.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Enviar por un canal no-op (DEMO_MODE) | Es una palanca de entorno, no por tenant; y no devuelve la respuesta al navegador |
| Conversación efímera sin persistir nada | Requiere manejar el historial aparte y duplica el camino del loop; el loop necesita una `Conversation` real |
| Escribir prompts de demo en código | Viola la invariante #3; el contenido de demo es dato (seed/AgentSpec) |

## Consecuencias

**Positivas:** demos sin canal y sin configurar packs; validar AgentSpec/playbooks al
instante; el mismo motor del runtime, sin caminos paralelos.

**Negativas / lo que aceptamos pagar:** el playground gasta tokens reales (se registran
en `usage_ledger` con la conversación de prueba, excluida de la cuota de costo); aparece
un canal `test` que hay que recordar filtrar en cualquier listado/métrica futuro.

**Qué haría que revisáramos esto:** que el canal `test` empiece a filtrarse mal en
métricas/listados, o que haga falta probar el pipeline completo (webhook, debounce,
envío), que el playground no cubre por diseño.

## Impacto en el código

`core/admin/playground.py` (nuevo), `core/admin/templates/asistente_probar.html`
(nuevo), `core/admin/views.py` (rutas `/admin/asistente/probar*`),
`core/admin/permissions.py` (prefijo → sección `connections`),
`core/admin/templates/base.html` (acento + menú),
`core/admin/templates/conexiones.html` (banner),
`core/shared/test_mode.py` (canal de prueba, nuevo),
`core/orchestrator/escalation.py` (guard de test),
`core/billing/metrics.py` (exclusiones), `tests/test_test_mode.py`.
