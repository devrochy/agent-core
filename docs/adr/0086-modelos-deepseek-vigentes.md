# ADR-0086 · Modelos DeepSeek vigentes (V4) y modo thinking

**Estado**: Aceptada · **Fecha**: 2026-10-02 · **Supersede parcialmente**: ADR-003

## Contexto

El ADR-003 fijó `deepseek-chat` (default) y `deepseek-reasoner` (razonamiento) como
modelos del framework. DeepSeek **discontinuó ambos nombres el 2026-07-24 15:59 UTC**:
las peticiones que los usen dejan de resolver (N-01, HU-ARCH-10). Cualquier entorno
con el default viejo falla por completo.

Además, el comportamiento de los IDs V4 no es equivalente por defecto: los modelos
explícitos (`deepseek-flash`, `deepseek-v4-pro`) **encienden thinking**, mientras
`deepseek-chat` era el modo *no-thinking*. Un renombre mecánico cambia latencia,
tokens y costo.

## Decisión

1. **Modelo por defecto: `deepseek-flash`** (P-05/D-22), con `thinking: {type:
   "disabled"}` en el turno normal para reproducir el comportamiento del viejo chat.
2. **Razonamiento: el mismo `deepseek-flash`** con `thinking: {type: "enabled"}`,
   solo cuando el difficulty router lo decide y el turno no requiere tools.
3. **Respaldo: `gpt-4o-mini`** ante error del proveedor o fallas de tools > 5 %/h.
   **No** usar Gemini 2.5 Flash-Lite (se retira 2026-10-20). `deepseek-v4-pro` es un
   upgrade deliberado, no el sucesor automático de `deepseek-reasoner`.
4. **Precios vigentes** en `PRICING`: `deepseek-flash` = $0.14/$0.28 por MTok;
   `deepseek-v4-pro` = $0.435/$0.87.
5. **Migración de datos** (`0043`): los `AgentSpec` con `deepseek-chat`/`reasoner`
   pasan a `deepseek-flash`, auditado.

## Justificación

- `deepseek-flash` es ≈2× más barato que el viejo `deepseek-chat` y habilita
  multimodalidad nativa.
- Fijar `thinking` explícitamente hace la migración auditable y evita pagar tokens de
  razonamiento en cada respuesta.

## Consecuencias

- (+) El default no depende de un nombre retirado; el costo baja.
- (+) Un solo modelo con dos modos simplifica el routing y el respaldo.
- (−) El pricing sigue hardcodeado en `PRICING` (actualización manual; ver HU-BILL-03).
- **Qué haría que revisáramos esto**: que DeepSeek cambie los IDs/contratos de V4, o
  que las evals de tool calling de `deepseek-flash` caigan por debajo del respaldo.
