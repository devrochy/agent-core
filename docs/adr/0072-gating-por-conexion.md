# ADR-0072 — Gating declarativo de tools por prerequisitos de conexión

- **Estado:** aceptado
- **Fecha:** 2026-09-25

## Contexto

El enablement por pack (`AgentSpec.packs`, ADR-0019) decide qué tools ve el LLM.
Pero un pack puede estar habilitado sin su conexión configurada: `pack_calendar`
habilitado y sin cuenta de Google conectada deja `create_calendar_event`/
`check_availability` ofrecidas al modelo, que las llamaría y fallaría (o prometería
una cita sin poder crearla). El enablement por pack es *consentimiento*; la conexión
es un *prerequisito operativo*, la misma familia que la salud por tenant (ADR-0024).

## Decisión

`MCPTool` gana `requires_connection_types: list[str]` (atributo del core, no del
contrato congelado). Una tool solo se ofrece/ejecuta si el tenant tiene **activas**
todas las conexiones de esos tipos (ADR-0069). Se aplica en **dos** puntos del loop:

1. **Pre-prompt**: se filtran las tools candidatas antes de `resolve_flow`/router,
   así tampoco entran al flujo de atención (ADR-0054) ni al prompt.
2. **`_tool_denial`**: si la conexión se revoca a mitad de turno (el LLM ya tenía la
   tool en contexto), el `tool_call` se deniega con un mensaje claro — extensión de
   ADR-0054, que declara a `_tool_denial` como la compuerta única de ejecución.

Es ortogonal a `AgentSpec.packs` (consentimiento), a la salud por tenant (ADR-0024)
y a `scopes` (autorización fina, ADR-0050). Las tools de calendario declaran
`["google_calendar"]`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dejar que la tool falle de forma recuperable | El LLM gasta un turno y hay que explicarle el error en el prompt; peor UX y más tokens |
| Filtrar solo en el pre-prompt | No cubre la revocación a mitad de turno (tool_call ya emitido) |
| Un flag por tool en `AgentSpec` | La conexión ya existe como dato; duplicar el estado en el spec lo desincroniza |
| Gate por dominio de red | No distingue "conectado" de "permitido"; el prerequisito es la conexión |

## Consecuencias

**Positivas:** "sin conexión el asistente no agenda" es determinístico, no depende
de que el prompt se porte bien; reutilizable por cualquier pack con conexión.

**Negativas / lo que aceptamos pagar:** una query por turno para resolver los tipos
conectados del tenant; el atributo es core (hay que mantenerlo fuera de los
contratos).

**Qué haría que revisáramos esto:** que un prerequisito no sea "tener una conexión
de tipo X" (p. ej. una cuota o una feature flag) → se modelaría como otra dimensión.

## Impacto en el código

- `core/mcp/types.py` (`MCPTool`), `core/orchestrator/loop.py` (filtro + `_tool_denial`),
  `core/gateway/connections.py` (`active_connection_type_ids`).
- `packages/pack-calendar/pack_calendar/skills.py` (declara el prerequisito).
- Sin cambios en `packages/contracts`.
