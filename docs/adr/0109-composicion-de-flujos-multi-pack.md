# ADR-0109 — Composición de flujos multi-pack

- **Estado:** aceptado
- **Fecha:** 2026-10-08
- **Enmienda:** ADR-0054 (flujo de atención por pack y tenant): el `flow.json` del negocio deja de ser
  autoritativo; el flujo efectivo se compone.
- **Contexto:** HU-FWK-01 (SPEC-05, decisión D-SPEC-05-2: IDs con namespace por pack, secciones `tenant`
  intocables y tools del core siempre disponibles).

## Contexto

Con varios packs, el flujo de ADR-0054 rompía cosas (verificado en `develop` el 2026-10-08):

- Con un flujo declarado, `loop.py` solo ofrecía las tools listadas en él: quedaban fuera
  `update_customer_profile`, `remember_fact`, `send_email` y las `sub_*` del core (HU-SUB-01).
- `saludo` existía en `pack_commerce` y `pack_cobranza`; `_dedupe_sections` dejaba la primera por orden
  alfabético, así que un negocio con ambos perdía `crear_lead`, `consultar_inventario`, etc.
- El `flow.json` del negocio era autoritativo y se sembraba copiando las secciones de los packs: las copias
  quedaban viejas. En test, `solvershub` tenía `sub_gestion` con los nombres de antes de HU-SUB-01 y el
  asistente perdió esas tools. Un pack activado después no aportaba sus secciones.
- `seed_tenant_flow` decidía qué pisar por el campo `pack`: una sección propia etiquetada `pack_commerce`
  (`planes` de `solvershub`) se perdía en el re-seed.

## Decisión

El flujo efectivo se **compone** en cada turno y cada sección tiene un dueño:

1. **Core** (`core/flow.json`): `core:saludo` y `core:escalamiento`, una sola vez para todo negocio. Un pack
   que declare `saludo`, `escalamiento` o una sección con `pack: "core"` se ignora.
2. **Packs** (`packages/pack-*/flow.json`): el dueño es el pack de cuyo archivo sale la sección; su id
   calificado es `pack_x:id`, así dos packs no chocan. Se incluyen **todos** los packs habilitados, siempre.
3. **Negocio** (`TENANTS_ROOT/<slug>/flow.json`): solo sus secciones `tenant` y, si quiere, un override
   explícito de una sección del core o de un pack (mismo id calificado). Una sección etiquetada con un pack
   cuyo id ese pack no declara es del negocio. Una copia vieja de una sección de pack (sus tools son del
   default o no existen en ningún `flow.json`) se ignora: solo cuenta como override si trae tools de otras
   secciones.

Orden: `core:saludo` → secciones de los packs habilitados → secciones `tenant` → `core:escalamiento`.

**Tools del core siempre disponibles:** `MCPTool.always_available` (todas las de `CoreSkill` y las `sub_*`
de `SubscriptionSkill`). El loop las ofrece aunque ninguna sección las nombre; siguen sujetas a
`requires_connection_types`/`connection_predicate` (las `sub_*` solo con un proveedor de suscripciones).

**Siembra y migración:** el alta ya no copia secciones de packs al archivo del negocio.
`normalize_tenant_flow` (y `agent-core flow migrate <slug> [--apply]`) quita las copias de packs, convierte
en `tenant` las secciones que su pack no declara y avisa de overrides y de tools que ningún `flow.json`
declara. Ids heredados: `pack_commerce:saludo` → `pack_commerce:descubrimiento`,
`pack_cobranza:saludo` → `pack_cobranza:verificacion`, `escalamiento` → `core:escalamiento`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener el archivo del negocio autoritativo y re-sembrar mejor | Las copias siempre envejecen: cada cambio de un pack exige re-sembrar todos los negocios. |
| Un único flujo plano por negocio (D-SPEC-05-2, opción B) | Vuelven los choques al agregar packs. |
| Que las tools del core se listen en cada sección de cada pack | Repite la lista en cada pack y basta un olvido para ocultarlas. |

## Consecuencias

**Positivas:** agregar un pack nunca oculta tools de otro ni del core; los cambios de un pack llegan a todos
los negocios sin re-sembrar; las secciones propias del negocio no se pierden.

**Negativas / lo que aceptamos pagar:** un negocio ya no puede **quitar** una sección entera de un pack solo
borrándola de su archivo (hay que deshabilitar el pack o hacer un override con otras tools); el escalamiento
pierde los matices por pack («gestor» en cobranza): el texto del core es general.

**Qué haría que revisáramos esto:** que un negocio necesite ocultar una sección de un pack habilitado (agregar
una lista `ocultar` al archivo del negocio), o que el escalamiento genérico baje los golden sets de cobranza.

## Impacto en el código

- `core/agents/flow.py` (composición, `normalize_tenant_flow`), `core/flow.json` (nuevo).
- `packages/pack-commerce/flow.json`, `packages/pack-cobranza/flow.json` (ids renombrados, sin escalamiento).
- `core/mcp/types.py` (`always_available`), `core/tools/core_skill.py`, `core/subscriptions/tools.py`,
  `core/orchestrator/loop.py`.
- `core/onboarding.py`, `cli/commands/flow.py` (`flow migrate`).
