# ADR-0162 — Herramientas de análisis de la vertical inmobiliaria

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-32 a 36, 39, 41 y 42; D-44 y D-SPEC-28-10 de solvershub-docs; P-33. **Usa:** ADR-0017 (copy en configuración), ADR-0119 (verificador y `_safe_append`) y ADR-0156.

## Contexto

D-44 suma simulador de crédito, precio por comparables (CMA), rentabilidad, entorno, desplazamiento, comparación y apoyo de negociación. El verificador de hoy solo comprueba montos completos en pesos bajo `MONEY_KEYS`. No verifica porcentajes, minutos, m² ni cifras abreviadas, y las afirmaciones de la vertical llegan en la fase 5 (HU-INMO-27). La simulación tomada como oferta y el rango tomado como avalúo son riesgos legales.

## Decisión

Las herramientas de análisis son tools de lectura **deterministas** del pack, con parámetros del negocio y salida en **bloque determinista**.

1. **Bloque determinista hasta la fase 5.**
   - Cifras, diferencias y el aviso obligatorio («simulación informativa, no es una oferta de crédito»; «orientativo, no es un avalúo») los arma el servidor, con el molde de `_safe_append`.
   - Los textos salen de configuración (ADR-0017).
   - El LLM pone una frase de entrada y no repite cifras; un golden lo comprueba.
   - Los montos van además bajo `monto`/`total` para el verificador actual.
2. **Cálculo en el servidor.**
   - Las cifras derivadas (Δ entre inmuebles, inicial + gastos, año de equilibrio) las calcula el servidor; el modelo nunca suma ni resta.
   - Las tools devuelven solo valores redondeados: 100 m y 5 min.
3. **Parámetros con fuente y fecha** (P-33).
   - Tasas por producto, % financiable, UVR, inflación supuesta, gastos de compra y costos del arriendo son `InmobiliarioConfig.analisis`, que carga el negocio con fuente y fecha.
   - El panel avisa a los 30 días.
   - No hay constantes en el código ni integración automática en v1.
4. **CMA solo con el inventario del negocio.**
   - Comparables en `cma_comparables`, con FK compuesta `(tenant_id, inmueble_id)`.
   - Al cliente y al propietario solo se les da rango, `n` y confianza, nunca la lista ni precios de cierre.
   - El informe y el detalle son solo del asesor.
5. **Sin asesoría.**
   - No hay preaprobación, tasas de un banco concreto ni estudio de crédito: esos pedidos van al asesor.
   - El avalúo certificado (Ley 1673) queda fuera.
6. **La banda de negociación nunca entra al turno.**
   - La calcula `inmo_registrar_oferta` y se guarda en `oportunidad_eventos`.
   - Solo la ve el asesor.
   - Nunca va a `escalations` del core.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Redacción libre con verificador ampliado ya | Ampliar `MONEY_KEYS` y agregar parsers es un cambio del core que hoy no tiene más consumidores. Llega con HU-INMO-27 |
| Que el modelo estime cuotas o precios | Viola la constitución §1.2.1: cifras inverificables |
| Integrar tasas automáticas (Banco de la República, Superfinanciera) | Una integración más que mantener, y las tasas publicadas son promedios, no ofertas |

## Consecuencias

**Positivas:** respuestas de decisión dentro del chat con cifras verificables y avisos que no dependen del modelo.

**Negativas / lo que aceptamos pagar:**
- Respuestas menos "naturales" mientras rige el bloque.
- Parámetros que el negocio debe mantener al día.

**Qué haría que revisáramos esto:**
- Que HU-INMO-27 esté en producción con afirmaciones de porcentaje, minutos y cifras abreviadas: entonces la redacción libre se habilita por negocio.
- Que más del 20 % de los parámetros del piloto pasen 30 días sin actualizar.

## Impacto en el código

- `packages/pack-inmobiliario/` (tools `inmo_simular_credito`, `inmo_gastos_compra`, `inmo_arrendar_vs_comprar`, `inmo_rentabilidad`, `inmo_precio_sugerido`, `inmo_entorno`, `inmo_desplazamiento`, `inmo_comparar`, `inmo_registrar_oferta`).
- `core/orchestrator/safe_renderer.py` (bloque).
