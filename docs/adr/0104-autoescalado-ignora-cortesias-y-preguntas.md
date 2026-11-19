# ADR-0104 — El autoescalado por turnos sin resolver ignora cortesías y preguntas de avance

- **Estado:** aceptado
- **Fecha:** 2026-10-07
- **Enmienda:** ADR-0051: la definición de "turno sin resolver" deja de ser `resolved = bool(all_tool_results)`.
- **Contexto:** issue #234; incidente del 2026-10-05 en test (tres "Hola" seguidos escalaron la conversación de Rob y pausaron el bot).

## Contexto

El autoescalado por turnos sin resolver (ADR-0051) contaba como "no resuelto" todo turno del asistente sin tools:
`resolved=bool(all_tool_results)` en `loop.py`. Eso incluía:

- saludos y cortesías del cliente ("Hola", "Gracias") — el asistente responde sin tools y el contador subía;
- turnos en que el asistente **avanza** pidiendo un dato o confirmando ("¿Para qué día quieres la demo?") — también
  sin tools, también subían.

Tres "Hola" seguidos escalaban la conversación y pausaban el bot (`turns_unresolved`). La mitigación fue subir el
umbral del tenant de test a 5, que es un parche por configuración, no la corrección del conteo.

## Decisión

Un turno sin tools **no suma** al contador cuando la conversación está avanzando por otra vía:

1. **Cortesía suelta.** El mensaje del cliente es un saludo, agradecimiento o confirmación sueltos
   (`_es_cortesia`): "hola", "buenas tardes", "gracias", "ok", "listo"…
2. **Pregunta de avance.** La respuesta del asistente contiene una pregunta (`_pregunta_de_avance`): pide un dato,
   confirma o propone alternativa.

En ambos casos el contador queda **como estaba**: no suma y tampoco reinicia (reiniciarlo permitiría "escalar
infinitamente" con preguntas). El helper `_turno_avanza_sin_tools` centraliza la regla; la respuesta neutral del
guardrail sigue contando como no resuelta (ADR-0092 — es la red de un fallo real).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Contar solo turnos consecutivos con el mismo intento sin tools | Es la alternativa del issue, pero exige comparar intenciones entre turnos (normalización, similitud) y el incidente era de cortesías: más superficie para el mismo arreglo |
| Que la pregunta **reinicie** el contador | Un asistente defectuoso que pregunte en bucle nunca escalaría; "no sumar" conserva la red |
| Subir el umbral por configuración (lo aplicado en test) | Parcha el síntoma por tenant y deja el mismo conteo incorrecto para todos los demás |

## Consecuencias

**Positivas:** los saludos y los ciclos normales de pregunta/respuesta no pausan el bot; el umbral vuelve a
significar "el cliente insiste sin que el asistente avance".

**Negativas / lo que aceptamos pagar:**
- Un asistente que responda sin tools, sin preguntar y a un cliente que no sea cortés sigue sumando (correcto).
- La detección de cortesía es por patrón del mensaje completo: un "hola" con una petición ("hola, quiero una demo")
  no es cortesía y cuenta si el asistente no avanza.

**Qué haría que revisáramos esto:** evals que muestren falsos positivos/negativos del patrón, o un estado de
conversación más rico (intención) cuando exista.

## Impacto en el código

- `core/orchestrator/loop.py`: `_CORTESIA`, `_es_cortesia`, `_pregunta_de_avance`, `_turno_avanza_sin_tools`; el
  turno final pasa `count_unresolved`; `_close_turn` y `_finish_turn` aceptan `count_unresolved`.
- Tests: `tests/test_autoescalado_cortesias.py`.
