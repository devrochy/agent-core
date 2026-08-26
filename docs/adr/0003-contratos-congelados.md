# ADR-0003 — Contratos congelados; cambios solo por ADR

- **Estado:** reemplazado por ADR-0011
- **Fecha:** 2026-08-26

## Contexto

Un framework se define por sus contratos, no por su implementación. Si los tipos compartidos
cambian con cada feature, cada pack y cada tenant se convierte en un caso especial y la
promesa de "instalar un cliente sin escribir código" deja de sostenerse.

## Decisión

`@agentcore/contracts` expone cinco tipos: `InboundMessage`, `AgentSpec`, `ToolDefinition`,
`ToolContext`, `AgentTurnResult`. Se etiquetan `v1.0.0` y son inmutables. Cualquier cambio
que altere su forma requiere un ADR y un incremento **major**.

El paquete no depende de nada más del monorepo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tipos que evolucionan libremente | Cada pack se vuelve incompatible con el siguiente; imposible versionar packs |
| Contratos por pack | No hay framework: hay N integraciones |

## Consecuencias

**Positivas:** los packs declaran `coreApi` y la compatibilidad es verificable; se puede
publicar un marketplace interno.

**Negativas:** añadir un campo obliga a pensarlo dos veces; a veces habrá que llevar un
campo en `custom` durante un ciclo antes de promoverlo al contrato.

**Qué haría que revisáramos esto:** tres packs consecutivos necesitando el mismo campo en
`custom` — señal de que pertenece al contrato.

## Impacto en el código

`packages/contracts`, y por transitividad todo lo demás.
