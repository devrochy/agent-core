# ADR-0011 — Runtime y contratos en Python; `@agentcore/contracts` redefinido en Python

- **Estado:** aceptado
- **Fecha:** 2026-08-26
- **Reemplaza:** ADR-0003

## Contexto

ADR-0003 congela los contratos en `@agentcore/contracts` (TypeScript, v1.0.0). El runtime
real del monorepo es Python (FastAPI, SQLAlchemy, Celery, Redis): ~15.000 líneas de core,
cli, plugins y tests, y ningún consumidor JavaScript. Mantener los contratos en TS obliga a
una doble fuente de verdad y a que packs Python definan sus tipos a mano sin verificación
contra el spec. Con un solo desarrollador, sostener dos mundos de lenguaje duplica el costo
de cada cambio sin beneficio.

## Decisión

Los cinco contratos (`InboundMessage`, `AgentSpec`, `ToolDefinition`, `ToolContext`,
`AgentTurnResult`) se definen y congelan en un paquete **Python** `@agentcore/contracts`,
sin dependencias, con la misma forma de la v1.0.0. El runtime es Python. El paquete TS actual
pasa a legacy/referencia. La regla de ADR-0003 se preserva: cualquier cambio de forma exige
un ADR nuevo y un incremento **major**.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener los contratos en TS | Doble fuente de verdad; los packs Python no pueden verificar compatibilidad contra el spec en compilación |
| Contratos por pack | No hay framework: hay N integraciones (motivo original de ADR-0003) |
| Sin paquete de contratos | Los packs dependerían de las firmas internas del core; imposible versionar packs |

## Consecuencias

**Positivas:** una sola fuente de verdad compilable contra el runtime; los packs Python
verifican su `coreApi` con import real; el migrador y el inventario trabajan en el mismo
lenguaje que el código.

**Negativas / lo que aceptamos pagar:** el paquete TS queda obsoleto y hay que migrar los
5 contratos a Python y volverlos a congelar; quien quisiera consumir desde JS no tiene tipos
(no hay consumidores JS hoy).

**Qué haría que revisáramos esto:** un consumidor real que exija tipos TypeScript derivados
de los contratos (p. ej. un frontend externo al panel) — señal para publicar tipos TS
generados, no para volver a la fuente en TS.

## Impacto en el código

`packages/contracts` (TS → Python, mismo nombre de paquete), `docs/adr/0003-contratos-congelados.md`
(estado → reemplazado por ADR-0011), `CLAUDE.md` (estructura y gate de calidad),
`docs/ROADMAP.md` (Sprint 0), y por transitividad todos los packs (`packages/pack-*`,
`packages/channel-*`, en Python).