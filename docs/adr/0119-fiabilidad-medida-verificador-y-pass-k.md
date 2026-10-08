# ADR-0119 — Fiabilidad medida: verificador de afirmaciones y evals de consistencia (pass^k)

- **Estado:** propuesto
- **Fecha:** 2026-10-08
- **Contexto:** HU-ARCH-28 y HU-ARCH-29 (SPEC-02, Hito 0A, decisión D-28), HU-AOS-06, HU-AOS-21 y HU-AOS-31 (SPEC-26).
  Enmienda ADR-0051 §4 (evals persistidos) y ADR-0025 (las frases de cierre del negocio las lee el verificador).
  Usa ADR-0092 sin cambiarlo. **Es el primero de la serie que se acepta**, porque lo usan HU del Hito 0A.

## Contexto

- La línea base de evals (34/35 en el framework, 7/8 en `solvershub`) mide **un** intento por escenario.
- El gate de CI falla a veces y se relanza a ciegas.
- El runner mira solo el primer paso del modelo.
- `crm_amount_check`, `crm_completion_check` y `calendar_completion_check` escalan compras válidas. Qué pasa al fallar lo configura cada negocio (`guardrail_fail_action`, ADR-0092; `escalate` por defecto).
- τ-bench (Sierra, 2024) muestra que los agentes de atención al cliente pierden mucha consistencia cuando el mismo caso se repite k veces.

## Decisión

La consistencia se mide con pass^k sobre conversaciones simuladas, y lo que dice el asistente se verifica contra la evidencia de forma determinista.

1. **Evals de consistencia:**
   - Escenarios con usuario simulado (objetivo, datos, estilo y semilla), datos sembrados, **aserciones sobre el estado final de la base** y tope de turnos, corridos contra el loop real sin efectos.
   - k intentos (4 en el nocturno, 8 antes de una release); se guardan pass@1 y **pass^k** (columnas aditivas en `eval_runs` y `eval_results`).
   - Los escenarios inestables se marcan con sus intentos fallidos.
   - En el Hito 0A el umbral vigente (≥ 95 % durante tres noches) no cambia y pass^4 se reporta.
   - Desde la Fase 1, el gate de release exige pass^4 ≥ 0,85 por procedimiento tocado y no-regresión (umbral inicial, se ajusta con la primera medición).
   - En el 0A corren contra una base efímera (CI y stack local), así que su consumo no llega al ledger de ningún negocio real. La marca `kind = eval` en `usage_ledger` llega con HU-AOS-02.
2. **Verificador de afirmaciones**, determinista:
   - Extrae montos, links, fechas y horas, referencias, productos y cantidades, y frases de cierre: las que declara la habilidad y las `completion_claims_custom` del negocio (ADR-0025, siempre literales).
   - Los compara con la evidencia: acciones del turno y de turnos anteriores (`audit_log`) y, desde la Fase 1, el estado de las tareas. También exige las reglas `required_tool` (ADR-0051) y respeta `disabled_guardrails`.
   - Ante un fallo: reintento correctivo (`guardrail_max_retries`) y luego la acción que configuró el negocio (`guardrail_fail_action`, ADR-0092: neutral o escalar). La mejora frente a hoy viene de menos falsos positivos, no de cambiar esa configuración.
   - Reemplaza los tres guardrails heurísticos solo cuando los falsos positivos son ≤ 2 % en un set etiquetado.
3. **LLM-juez en producción** (Fase 1): solo calibrado contra conversaciones etiquetadas por personas, con un acuerdo mínimo. Nunca decide en línea, solo audita.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Seguir con pass@1 | Oculta la inconsistencia: un error de 1 de cada 4 veces no se ve |
| LLM-juez en línea por turno | Costo, latencia y no determinismo en el camino del cliente |
| Mantener los guardrails heurísticos | Escalan compras válidas y pausan el bot |
| Pasar a respuesta neutral por defecto | Es una decisión de cada negocio (ADR-0092); el problema medido son los falsos positivos |

## Consecuencias

**Positivas:** la consistencia se mide y se exige; menos escalamientos falsos; cada fallo deja evidencia para corregirlo.

**Negativas / lo que aceptamos pagar:** más cómputo en el nocturno (en base efímera, no se factura a ningún negocio); parsers por tipo de afirmación; mantener el set etiquetado.

**Qué haría que revisáramos esto:** que pass^k alto no se correlacione con la calidad en producción (el simulador no es realista) o que el verificador siga con falsos positivos por encima del 2 %.

## Impacto en el código

- `evals/runner.py` y `evals/journeys.py`: simulador, k y aserciones.
- Migración aditiva de `eval_runs` y `eval_results`.
- Verificador en el core, integrado en `core/orchestrator/loop.py`. Lee `behavior_config` (ADR-0092) y la configuración de cada pack (`completion_claims_custom`).
- Retiro de los tres guardrails tras medir.
