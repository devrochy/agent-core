---
name: new-tool
description: >
  Crea una tool (MCPTool) de agent-core en un pack o en el core. Usar cuando se pida "crear
  una tool", "que el agente pueda X", "necesito una herramienta para", o cuando haya que
  exponer una capacidad nueva al modelo.
argument-hint: "<pack, ej. pack-commerce> <nombre_tool>"
---

Crear la tool `$1` en `packages/$0` (si faltan los argumentos, preguntar el pack y el
nombre antes de empezar).

Una tool es un `MCPTool` (`core/mcp/types.py`) que devuelve una `BaseSkill` del pack y
que el módulo registra en `on_activate`. Referencia viva: `packages/pack-subscriptions/`
(`skills.py`, `module.py`, `flow.json`).

## Pasos

1. **Declaración** en `packages/$0/pack_<x>/skills.py`, dentro de `get_tools()`:
   ```python
   MCPTool(
       name="<prefijo>_<verbo>_<objeto>",
       description=(
           "Qué hace. Cuándo usarla. Cuándo NO usarla."
       ),
       parameters={
           "type": "object",
           "properties": {"pedido_id": {"type": "string", "description": "..."}},
           "required": ["pedido_id"],
       },
       handler=self.<metodo>,
   )
   ```
   - `description` la lee el modelo en producción: es producto, no un comentario.
     Máximo 3 líneas.
   - `parameters` es JSON Schema estricto: tipos, `enum` donde aplique, `required`. **Nunca
     pidas al modelo un dato que el servidor ya conoce o debe decidir**: montos, precios,
     referencias de pago y `tenant_id` salen del servidor (ver los defectos D1 de commerce y
     13 de cobranza).
   - `requires_connection_types` / `connection_predicate` si la tool solo sirve con una
     conexión (ej. `["email_smtp"]` en `core/tools/core_skill.py`). `scopes` (ADR-0019) solo
     si el pack ya los usa.
2. **Handler** en la misma clase:
   ```python
   async def <metodo>(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
       async with get_session() as session:   # core.db.session: ya corre bajo RLS
           ...  # filtrar también por get_current_tenant_id() (core.shared.tenant)
       return {...}  # solo lo que el modelo necesita para responder, sin PII de más
   ```
   - Datos solo vía `get_session()`. Red solo vía `context.http` (`ScopedHttp`, allowlist
     de dominios del pack). Nunca `os.environ`, nunca el pool de Postgres.
   - `context.customer_id`, `context.conversation_id` y `context.idempotency_key` los
     inyecta el core; el LLM no los ve ni los manda.
   - **Errores de negocio = resultado, no excepción.** Monto bajo el mínimo del proveedor,
     cliente inexistente, validación: capturar (`PaymentCapabilityError`, etc.) y devolver
     `{"error": "<codigo_snake>", "mensaje": "<qué decirle al cliente>"}`. Hoy cualquier
     excepción que se escape **desactiva el pack completo para el tenant**
     (`core/orchestrator/loop.py:670-675`, ADR-0024; HU-ARCH-18 lo acota).
   - Escrituras idempotentes: usar `context.idempotency_key` (o una clave natural) para
     que un reintento no duplique el efecto.
   - Cambios de estado de dominio: emitir el evento con
     `await event_bus.emit(<CONSTANTE>, **payload)` (`core/shared/events.py`) e incluir
     `tenant_id` en el payload. El bus todavía no es durable (HU-SUB-03): los handlers
     tienen que ser idempotentes.
3. **Registro**: `on_activate(self, host)` del módulo ya hace `host.register_skill(...)`;
   si la skill es nueva, registrarla ahí. Guardrails con
   `host.register_guardrail(nombre, fn, priority)`; suscripciones a eventos con
   `host.subscribe(evento, handler)`. Nada de imports de otro pack: coordinar por los
   registros del core (`payment_registry`, `subscription_registry`) y eventos.
4. **Flujo y playbook**: agregar la tool a la sección que corresponda en
   `packages/$0/flow.json` (`"tools": [...]`) y explicar en el playbook del pack
   (`packages/$0/playbooks/*.md`) cuándo usarla. Sin esto el asistente no la ofrece en
   esa etapa del flujo. Los prompts nunca van en `.py`.
5. **Confirmación humana**: si la tool cobra, cancela, borra o es irreversible, se
   configura `requiresConfirmation` en su binding del AgentSpec (ADR-0050, se edita en
   Configuración del panel), y la descripción dice "solo cuando el cliente lo pida y
   confirme".
6. **Tests**:
   - unitario `tests/test_<pack>_<tool>.py`: caso feliz, entrada inválida, error de
     negocio devuelto como resultado (sin excepción), idempotencia (dos llamadas con la
     misma clave = un efecto);
   - si escribe en BD, integración en `tests/integration/` con un caso **cross-tenant**:
     el tenant B no ve ni modifica lo del tenant A.
7. **Eval**: un escenario en `evals/golden_scenarios/NN-<tema>.json` con
   `requires_plugins`, la conversación y `assertions` (`must_call_tool`,
   `must_not_contain`, `max_response_chars`). Si es del dinero o del cierre de venta,
   proponer sumarlo al gate del CI (`ci.yml`, lista `"06,07,08,21,…"`).

## Nombres

- Español, `snake_case`, `<verbo>_<objeto>`: `consultar_inventario`, `crear_pedido`.
- Con prefijo del pack cuando el verbo puede chocar con el de otro pack: `sub_link_pago`,
  `cob_registrar_abono`. Los nombres de tool son globales en la lista que ve el modelo.
- Las tools en inglés de calendar (`check_availability`…) son heredadas: no se renombran
  porque romperían flujos, evals y AgentSpecs.
- Una tool que hace dos cosas son dos tools.

## Antes de terminar

`uv run ruff check core cli scripts alembic evals tests packages/$0` y `uv run pytest -q`
(más `AGENT_CORE_INTEGRATION=1 uv run pytest tests/integration -q` si toca BD) en verde, y
`uv run python evals/runner.py "<NN>"` con el escenario nuevo. El CI no lintea `packages/`:
hacerlo a mano. No la marques completa si algo falla. Si la tool toca datos de clientes
o dinero, correr el subagente `auditor-multitenant`.
