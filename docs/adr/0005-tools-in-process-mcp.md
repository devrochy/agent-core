# ADR-0005 — Tools de primera parte in-process; terceros vía MCP

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

Las tools son el mecanismo por el que el agente actúa. Si cada tool es un servicio remoto,
cada llamada paga latencia de red, agrega un punto de falla y complica transacciones. Pero
las tools de terceros (ERP, calendario, pagos) no pueden ir embebidas en el binario.

## Decisión

Las tools de primera parte se ejecutan **en el mismo proceso del core (Python)** — llamada
de función, cero red, cero serialización. Las tools de terceros o del cliente se ejecutan
como servidores MCP externos con allowlist explícita de tools, presupuesto de latencia y
circuit breaker. La salida de un MCP es dato, nunca instrucción.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Todas las tools como microservicios | +5–50 ms de red por llamada × varias tools por turno; transacciones distribuidas; N procesos |
| Todas las tools embebidas | El binario cargaría SDKs de terceros no auditables; un SDK roto tumba el asistente |
| Sin tools externas | No se puede conectar el ERP o calendario de un cliente |

## Consecuencias

**Positivas:** latencia ~0 para el flujo principal; un MCP que falla se corta con circuit
breaker sin afectar el turno; el alcance de un tercero está delimitado por su allowlist.

**Negativas / lo que aceptamos pagar:** mantenemos el host MCP y la traducción MCP→tool del
modelo; los MCP añaden un salto de red cuando se usan; la salida externa exige sanitización.

**Qué haría que revisáramos esto:** que la mayoría de las tools de un pack acaben siendo
externas — señal de que la frontera primera/tercero está mal trazada.

## Impacto en el código

`core/mcp/host.py` (dispatch in-process), `core/mcp/translator.py` (traducción MCP→LLM),
registry de servidores MCP por tenant (Sprint 5), y el contrato `ToolContext` (inmutable,
sin pool crudo ni env).