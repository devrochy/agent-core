# ADR-001 · Monolito Modular con Adaptadores Selectivos

**Estado**: Aceptada · **Fecha**: 2026-08-19

## Contexto

Se evaluó arquitectura hexagonal pura (puertos/adaptadores para todo), layered simple
y Clean Architecture para el core del framework.

## Decisión

Monolito modular en capas simples (gateway → orchestrator → mcp/memory → plugins)
con el patrón Adapter aplicado SOLO en los tres puntos con variabilidad real:

1. **LLM** (`BaseLLMProvider`): DeepSeek ↔ Anthropic ↔ OpenAI-compatible.
2. **Canal** (`InboundAdapter`/`OutboundAdapter`): WhatsApp hoy; Telegram/Instagram mañana.
3. **Cola** (`TaskQueue`): Celery hoy; Arq/RQ mañana.

El resto del core usa clases concretas, sin interfaces intermedias.

## Justificación

- La lógica de negocio vive en los **plugins**, no en el core: el core es plomería
  estable (enrutar, llamar LLM, ejecutar tools, arbitrar). Hexagonal protege dominios
  complejos que aquí no existen.
- Referencia probada: agent-sale (TypeScript) evolucionó 23 fases y 56 migraciones
  con exactamente este patrón, sin necesitar hexagonal.
- Python + duck typing + unittest.mock hacen innecesaria la capa extra de puertos
  para testear.
- DX: un developer de plugins lee 3 archivos (module/skills/config), no 10 interfaces.

## Consecuencias

- (+) Menos archivos, menos indirección, onboarding rápido.
- (+) Swappability garantizada exactamente donde se necesita.
- (−) Si el core creciera lógica de dominio propia, habría que extraerla a un plugin
  (regla: el core no acumula reglas de negocio).
