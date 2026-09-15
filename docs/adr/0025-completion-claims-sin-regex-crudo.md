# ADR-0025 — `completion_claims` custom por tenant: frases literales, nunca regex crudo

- **Estado:** aceptado
- **Fecha:** 2026-09-14

## Contexto

`crm_completion_check` (`packages/pack-commerce/pack_commerce/module.py`)
bloquea al modelo cuando afirma en texto haber completado una acción de
negocio ("cerré tu pedido", "un asesor te va a contactar") sin haber
llamado la tool correspondiente en ese turno — trabajado extensamente
esta sesión, incluyendo un incidente real donde el modelo esquivó la
primera versión del guardrail cambiando de conjugación del verbo. Hasta
ahora, `COMPLETION_CLAIMS` era un `dict[str, tuple[re.Pattern, set[str]]]`
FIJO en código: 4 claims, cada uno un regex compilado a mano y calibrado
contra los incidentes reales.

Pedido explícito del usuario (`docs/ROADMAP.md`, mapa de parametrización
por tenant): un negocio de otro rubro (ej. una clínica que agenda citas,
no pedidos) necesita sus PROPIOS claims — hoy eso exige tocar código.

## Decisión

Dos campos nuevos en `CRMConfig` (mismo patrón ya usado esta sesión para
parametrización por tenant sin tocar el contrato `AgentSpec`, JSONB
cifrado vía `plugin_manager.save_config`, sin migración de esquema):

1. `completion_claims_disabled: str` — CSV de nombres de claims
   incorporados a desactivar.
2. `completion_claims_custom: str` — un DSL de una línea por claim:
   `etiqueta: frase uno | frase dos => tool_uno, tool_dos`.

**La regla no negociable: `completion_claims_custom` NUNCA acepta regex
crudo.** El tenant escribe frases literales; `_parse_custom_completion_claims`
(`pack_commerce/module.py`) las escapa (`re.escape`) y las une con `|`
antes de compilar. Esto no es una limitación de producto, es la decisión
de seguridad central de este ADR: no existe en TODO el repo ningún
precedente de compilar un regex proveniente de config/BD/formulario admin
(confirmado por auditoría exhaustiva antes de diseñar esto — los únicos
`re.compile` del repo son constantes fijas en código), y no hay ninguna
infraestructura de mitigación de ReDoS (sin `re2`, sin timeout, sin
linter de complejidad de patrones). Aceptar regex libre de un admin —
aunque sea sin mala intención, un patrón simplemente "caro" — introduce
un vector de negación de servicio nuevo sin ninguna red de seguridad
existente. Alternancia de literales escapados no tiene backtracking
catastrófico posible: el riesgo queda eliminado por diseño, no mitigado.

Un claim custom cuyo nombre coincide con uno incorporado lo REEMPLAZA
(no se combinan) — permite a un tenant redefinir un claim existente si
su vertical necesita otras frases para el mismo concepto.

Fail-open ante config ausente o inválida (mismo criterio que el resto
de guardrails y que `_plugin_config()` en `skills.py`): si
`plugin_manager.load_config` falla por cualquier razón, el guardrail
sigue evaluando los claims incorporados de siempre — nunca debe romper
el turno del cliente por un problema de configuración.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Regex libre por tenant (campo `str` con el patrón tal cual) | Riesgo real de ReDoS sin ninguna mitigación existente en el repo — el vector más simple de introducir, el más caro de descubrir en producción |
| Soporte genérico de `list[BaseModel]` en `core/admin/form_generator.py` (array de objetos {frases, tools}) | El generador hoy no resuelve `$ref`/`$defs` de Pydantic ni sabe renderizar/parsear un array de objetos — confirmado que el único campo similar que ya existe (`CRMConfig.transfer_accounts: list[dict]`) está efectivamente roto vía UI. Construir esto es una feature de UI aparte (filas dinámicas agregar/quitar), no el alcance de este ítem. Se prefirió un DSL de texto que encaja en el generador de formulario TAL COMO ESTÁ (agregando solo un `format: "textarea"` genérico y reusable). |
| `re2` o librería de regex con límite de backtracking | Dependencia nueva para un caso que se resuelve sin ella (literales escapados no tienen el problema que `re2` mitiga) |

## Consecuencias

**Positivas:** un tenant de cualquier vertical puede definir sus propios
claims de "phantom completion" sin tocar código ni pedir un deploy;
desactivar un claim que no aplica a su negocio (ej. `link_de_pago` para
quien no vende en línea) es igual de simple. El campo `format: "textarea"`
agregado a `form_generator.py` es genérico — cualquier campo futuro de
texto largo lo puede reusar sin código nuevo.

**Negativas / lo que aceptamos pagar:** el DSL es más limitado que regex
real — solo alternancia de frases literales (sin rangos, sin lookahead,
sin cuantificadores). Es la limitación deliberada que elimina el riesgo;
no hay forma de tener ambas cosas (expresividad de regex completo Y
seguridad garantizada) sin construir un sanitizador de regex, que no
existe. `CRMConfig.transfer_accounts` sigue roto vía UI — no se arregló
de paso porque hubiera ampliado el alcance a la feature de UI ya
descartada arriba.

**Qué haría que revisáramos esto:** que un vertical necesite un patrón
que las frases literales no puedan expresar (ej. un rango numérico en el
texto) — ahí sí valdría la pena diseñar un sanitizador de regex real
(timeout + límite de longitud + linter de complejidad), en vez de seguir
extendiendo el DSL con más operadores ad-hoc.

## Impacto en el código

`packages/pack-commerce/pack_commerce/config.py` (`CRMConfig`, 2 campos
nuevos), `packages/pack-commerce/pack_commerce/module.py`
(`_parse_custom_completion_claims`, `_resolved_completion_claims`,
`_completion_guardrail`), `core/admin/form_generator.py` (`format:
"textarea"`).
