# ADR-002 · Plugins embebidos in-process vía entry_points

**Estado**: Aceptada · **Fecha**: 2026-08-19

## Contexto

Dos modelos posibles para los plugins: (a) servicios independientes (contenedor/proceso
por plugin, comunicación HTTP/gRPC) o (b) paquetes Python instalados en el mismo
proceso del core.

## Decisión

**(b) Código embebido.** Los plugins son paquetes pip detectados por
`importlib.metadata.entry_points(group="agent_core.plugins")`. Ejecutar una tool es
`await tool.handler(args, context)` — una llamada de función en el mismo intérprete.

## Justificación

- Latencia: un turno ejecuta ~1-5 tools; 5-50ms de red por tool contra ~0ms in-process
  es la diferencia entre un asistente ágil y uno torpe (requerimiento explícito del
  proyecto: "no quiero que se comunique al módulo para evitar latencia y sobrecarga").
- Operación: un VPS de 2GB corre todo. N plugins como servicios = N healthchecks,
  N deploys, service discovery.
- Transacciones simples (una sesión SQLAlchemy) en vez de patrones distribuidos.

## Aislamiento sin procesos

El riesgo del monolito ("un módulo roto tumba todo") se mitiga en software:
PluginError → auto-disable + aviso en panel; guardrails fail-open; eventos fire-safe;
cron guard por estado del plugin; frontera de imports estricta (PLUGIN-CONTRACT §13).

## Consecuencias

- (+) Cero latencia inter-módulo, operación trivial, transacciones simples.
- (−) Un plugin con fuga de memoria afecta el proceso (mitigación: monitoreo del
  contenedor + reinicio; los plugins no manejan estado global).
- (−) Conflictos de dependencias pip entre plugins (mitigación: plugins con deps
  mínimas; el contrato §14 lo exige).
