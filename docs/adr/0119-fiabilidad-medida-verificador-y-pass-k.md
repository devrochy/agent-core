# ADR-0119 — Fiabilidad medida: verificador de afirmaciones y evals de consistencia (pass^k)

- **Estado:** aceptado (2026-10-08, con HU-ARCH-28)
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

## Adenda de implementación (2026-10-09, HU-ARCH-29)

Lo que fijó la implementación del verificador, revisada por el agente `arquitecto` antes de escribir código:

- **Dónde vive:** `core/orchestrator/verificador/` (`evidencia.py` y `verificador.py`). Corre en `end_turn` después de
  las validaciones de persona y antes de los guardrails de packs, que ven el texto ya corregido. Comparte el contador de
  reintentos y `guardrail_fail_action` (ADR-0092). Es fail-open para montos y cierres: si lanza una excepción, el turno pasa y el
  veredicto queda como `error`, pero los links se quitan todos.
- **Punto de extensión para packs:** `MCPHost.register_afirmaciones(fn)` con `Afirmacion(label, patron, tools,
  basta_url)` en `core.mcp.types`. Es API de plugins, no `packages/contracts`. Se filtra por packs habilitados
  (ADR-0019) y se limpia en `remove_plugin`. La polaridad (preguntas, negaciones y condicionales) es español genérico
  y vive en el core, no en cada pack.
- **Links:** las URLs se comparan normalizadas (sin esquema ni `www.`, host en minúsculas) y se detectan también las
  que empiezan por `www.`. Una URL sin respaldo **se quita**, no se reemplaza, y no consume reintento: el link exacto lo entrega
  `_safe_append`. Son evidencia las URLs de tools del turno actual y las del contexto (prompt del sistema y mensajes
  del cliente). No lo son las de turnos anteriores, porque un link de pago pudo quedar vencido (ADR-0087), ni las de
  mensajes anteriores del asistente, porque lavarían un link inventado.
- **Montos:** `core.shared.dinero` (antes en pack-commerce). `MONEY_KEYS` es una convención del core para los packs.
  Las cifras abreviadas ("$1,8 millones") no se verifican.
- **Fechas y referencias:** se extraen y se cuentan en el veredicto, pero no bloquean (modo sombra) hasta tener
  incidentes que lo justifiquen.
- **`required_tool` (ADR-0051):** sin cambios. Sigue evaluándose en `run_guardrails` con su semántica actual.
- **Instrucciones de reintento:** secciones `verificador_<motivo>` del playbook `respuestas` (invariante 3). En código
  solo queda un texto genérico de respaldo.
- **Veredicto por turno:** una fila en `audit_log` (`actor = core:verificador`, `action = verificacion`) con códigos
  y conteos, sin texto. Se escribe desde una sola función (`registrar_veredicto`) para que HU-AOS-10 la mueva a
  `turn_outcomes`. Los turnos que escala una tool no llegan a `end_turn` y no dejan veredicto.
- **Interruptores:** `verificador_montos`, `verificador_cierre` y `verificador_links`. Los nombres viejos
  (`crm_amount_check`, `crm_completion_check`) se traducen solos (`normalizar_desactivados`), así un negocio que los
  había apagado los mantiene apagados.
- **Retiro de los guardrails:** `crm_amount_check` y `crm_completion_check` se retiran porque el set etiquetado
  (`evals/verificador/`, `python evals/verificador.py`) da falsos positivos ≤ 2 % y ningún falso negativo. Lo vigila
  un test de la suite unitaria. Medición del 2026-10-09: 205 correctas con 0 falsos positivos y 14 falsas con 0 falsos
  negativos; en los mismos borradores, los guardrails viejos tenían 16,7 % de falsos positivos. `calendar_completion_check` sigue registrado: no tuvo falsos positivos medidos y su
  respaldo depende del `status` de cada acción. Se mide y se retira en una HU siguiente.

## Adenda (2026-10-09): URLs de una tool que terminó con error (HU-SH-07)

Aprobada por el dueño el 2026-10-09. Las URLs que devuelve una tool **del turno actual** cuentan
como evidencia de links aunque la tool haya terminado con error (`_ok=False`): las dio el servidor,
no las inventó el modelo. Caso: `crear_pedido` responde `terminos_pendientes` con `error` y los
enlaces de términos, privacidad y DPA (ADR-0125). El verificador los quitaba todos y el cliente
aceptaba sin verlos.

De una tool fallida no cuentan las URLs de su texto libre (`error`, `hint`, `message`): podrían
repetir un argumento con una URL que inventó el modelo. Una tool fallida sigue sin respaldar nada más: no entra en `acciones_ok` (cierres) ni en
`urls_enlace` («te envío el link de pago»). Una URL que no devolvió ninguna tool se sigue quitando,
y las de turnos anteriores tampoco cuentan.

