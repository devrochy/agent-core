# HU-008 · Visibilidad de costos LLM

**Como** admin, **quiero** ver cuánto gasta el asistente en LLM, **para** controlar
el presupuesto y detectar anomalías.

## Criterios de aceptación

- [ ] Cada llamada al LLM registra en `llm_usage`: provider, model, tokens in/out,
      tokens de cache leídos, latencia y costo USD calculado con la tarifa del modelo.
- [ ] El registro es best-effort: si falla, la respuesta al cliente NO se interrumpe.
- [ ] /admin/costos muestra: costo de hoy, últimos 30 días, total de llamadas y
      desglose por modelo (llamadas, tokens, latencia promedio, costo).
- [ ] El Dashboard muestra el costo de 30 días como KPI.
- [ ] Las tarifas por modelo viven en un dict versionado
      (`PRICING` en `core/llm/openai_compatible.py` y `anthropic_provider.py`).
