# ADR-0015 — Semántica de presupuesto y corte del turno

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

`AgentSpec.policies` declara `maxToolCallsPerTurn` y `budgetUsdPerConversation`. Sin una
semántica definida, cada desarrollador decidiría cuándo cortar, escalar o seguir gastando, y
un cliente con presupuesto agotado seguiría acumulando costo en tokens.

## Decisión

- **`maxToolCallsPerTurn`**: límite técnico. Al alcanzarlo, el turno se corta y se responde
  con el texto de cierre (en BD/playbook, nunca hardcode). No escala: escalar aquí es ruido.
- **`budgetUsdPerConversation`**: límite monetario. Se mide contra
  `SUM(usage_ledger.cost_usd)` de la conversación **en la cabecera del turno, antes de cada
  llamada al LLM** (no al final: el exceso ya se habría gastado). Al superarlo:
  1. Se corta el turno con un mensaje de cierre.
  2. Se **marca la conversación** (`conversations.state["budget_exhausted"] = true`): los
     siguientes inbound no llaman al LLM hasta que un admin suba el presupuesto o lo resetee.
  3. Escalar a humano solo si `EscalationPolicy` del spec lo configura (no es comportamiento
     fijo del core).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Cortar solo (sin marcar la conversación) | Cada inbound nuevo sigue gastando tokens para responder "sin presupuesto" |
| Escalar siempre al agotar | Escalamiento automático por dinero sin config = ruido y costo de bandeja |
| Medir al final del turno | El exceso ya se gastó antes de enterarse |

## Consecuencias

**Positivas:** el gasto se acota antes de ocurrir; el flag de conversación hace el corte
permanente hasta decisión humana; el comportamiento es configurable vía spec.

**Negativas / lo que aceptamos pagar:** una lectura extra de `usage_ledger` por turno
(barata, RLS-scoped); el flag necesita un reset explícito del admin.

**Qué haría que revisáramos esto:** que el flag `budget_exhausted` se quede marcado tras
resetear el presupuesto — señal de que el ciclo reset/corte está mal definido.

## Impacto en el código

`core/orchestrator/loop.py` (cabecera del turno), `usage_ledger` (fuente de costo), y el
panel (reset de presupuesto por conversación).