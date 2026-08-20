# ADR-003 · DeepSeek como LLM por defecto + difficulty routing

**Estado**: Aceptada · **Fecha**: 2026-08-19

## Contexto

El framework necesita un default económico y capaz. agent-sale usa Claude (con DeepSeek
como respaldo real de operación); agent-dashboard usa DeepSeek para humanización.

## Decisión

1. **deepseek-chat** como modelo por defecto: function calling nativo, $0.27/$1.10
   por MTok, context caching automático por prefijo.
2. **deepseek-reasoner** para turnos de razonamiento profundo, PERO solo cuando el
   turno no requiere tools (R1 no soporta function calling).
3. **Difficulty routing** con heurísticas (regex de señales complejas, longitud,
   turnos sin resolver): sin llamada extra a un LLM clasificador. Asimétrico: "error
   hacia caro" (adaptado del ADR-023 de agent-sale).
4. Anthropic/OpenAI a un `.env` (o BYOK en panel) de distancia vía `BaseLLMProvider`.

## Justificación

- Un asistente PyME (~500 conv/mes) cuesta ~$3-6 USD/mes en DeepSeek vs ~$40-80 en
  Claude Sonnet. Para el mercado objetivo, el costo ES el producto.
- El contrato neutro (`system_blocks: list[str]`, tools JSON Schema) hace el upgrade
  a Claude un cambio de configuración, no de código.

## Consecuencias

- (+) Costo mínimo por defecto; calidad escalable por configuración.
- (−) deepseek-reasoner sin tools limita el routing (mitigado: el default con tools
  cubre el 95% de los turnos).
- (−) El pricing vive hardcodeado en `PRICING` (actualización manual documentada).
