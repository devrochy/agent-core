# ADR-0192 — Sin General: el Coordinador enruta a un especialista por defecto

- **Estado:** propuesto
- **Fecha:** 2026-11-12

## Contexto
El General nace en cada alta por `backfill_agentes`, que convierte el `AgentSpec` v2 con el lector
tolerante; ADR-0139 §3 deriva de él a los especialistas del preset (`derivar_contenido`) y el build
lo refresca desde v2 para que Configuración le llegue. Con ADR-0191 el runtime v2 y el lector se
retiran, y con D-68 (ADR-0190) comercio, agenda, cobranza y suscripciones ya son tipos de agente. El
dueño decidió el 2026-11-12 (D-69, opción C) retirar el General en el mismo tren (HU-AOS-68).

## Decisión
Un negocio tiene un Coordinador que decide y especialistas que atienden; uno de ellos es el agente
por defecto de cada conexión conversacional. No existe el agente `general`.

Detalle:
1. **Alta.** Nacen el Coordinador (KernelSpec por defecto y la voz del alta mapeada a sus campos, sin
   texto de prompt en el código) y sus instancias; el primer especialista lo trae el preset del tipo
   de negocio y queda `is_default` en cada conexión.
2. **Invariante de la conexión.** Una conexión conversacional puede existir pendiente, pero no tiene
   release activa ni queda habilitada sin un especialista `enabled`, `is_default` y con versión
   publicada. El build falla con `sin_especialista` en vez de crear una release a medias.
3. **Ruteo.** Sin tarea, regla ni disparador, el Coordinador enruta al especialista por defecto, nunca
   a un agente `core`.
4. **Derivación de especialistas.** El contenido inicial de un agente nuevo (desde plantilla o preset)
   sale de `settings` del negocio, los valores de la plantilla y, si existe, el primer agente del mismo
   tipo; ya no de `_base_del_general`.
5. **Configuración.** Identidad, Voz y Cerebro publican una versión del Coordinador o del especialista
   que corresponda (ADR-0191 §4).
6. **Claves.** `general` deja de ser una clave reservada; solo `coordinador` lo es. El panel habilita
   módulos por el tipo del agente publicado, no por su `key`.
7. **Datos.** Sin migración: test y producción se resiembran (D-68, ADR-0190 §10). El histórico de
   `usage_ledger` y `agent_record_origins` no se borra.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|
| Mantener el General y sembrarlo nativo en v3 | Portar persona, tools y playbooks de `convertir_v2` para tirarlo después; dos formas de atender |
| Plantilla «Recepción» como especialista general | Otro agente que mantener, decidido sin datos reales; queda como salida si el residuo genérico cae mal |
| Que el Coordinador conteste el residuo | El Coordinador es el kernel determinista (ADR-0112): decide, no conversa |

## Consecuencias
**Positivas:** un solo modelo de agentes; el panel y las métricas hablan de agentes reales; ADR-0191 no
necesita portar el lector tolerante ni el refresco del General.

**Negativas / lo que aceptamos pagar:** el especialista por defecto recibe saludos, preguntas ambiguas y
«quiero hablar con alguien»; el onboarding debe elegir el tipo más general del negocio. Presets, editor,
métricas y copy del plan cambian juntos.

**Qué haría que revisáramos esto:** que en el primer mes de producción más del 10 % de los turnos del
especialista por defecto acaben en escalamiento por estar fuera de su tipo; entonces se evalúa la
plantilla «Recepción».

## Impacto en el código
`core/alta_negocio.py`, `core/agents/{backfill_agentes,instancias,editor,versiones,data_por_agente}.py`,
`core/presets/{__init__,agentes}.py`, `core/releases/build.py`, `core/kernel/router.py`, `core/metricas/resumen_agentes.py`,
`core/billing/metrics.py`, `core/qa/datos_generados.py`, la API de agentes y resumen (`AgentOverviewItem`, `AgenteResumen`)
y `docs/openapi.json`; panel y APK.

Estado de ADR anteriores (nota, no edición): ADR-0139 §3 (`derivar_contenido` sin General), ADR-0171
(asistente de creación: General de coordinación) y la enmienda de ADR-0142 (el General no cuenta en el
plan) dejan de aplicar al General.
