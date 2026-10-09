## Journeys de un negocio y canary del asistente (HU-SH-08, 2026-10-09)

- `evals/journeys.py --tenant <slug>`: corre `TENANTS_ROOT/<slug>/evals/journeys/` contra una réplica efímera del
  asistente. Copia sus artefactos a un `TENANTS_ROOT` temporal, crea el negocio con su preset y su plan, sin
  ejemplos, y siembra catálogo, packs y voz de marca. Los escenarios del cliente siguen fuera del framework.
- `"vendedor": true` en `negocio.json`: el alta automática (HU-SH-03) corre de verdad dentro del proceso del
  runner, sin tocar la allowlist real.
- Paso nuevo `{"outbox": "despachar"}`: entrega los eventos del outbox del negocio del journey (solo ese negocio)
  para que la compra llegue hasta el alta. Los correos de sistema se guardan en vez de enviarse
  (`{correos.ultimo.para}`).
- `core.onboarding.sembrar_artefactos` (catálogo, `<pack>.json` y voz de marca), extraída de `agent-core tenant
  seed`, que ahora la usa. `core.tasks.domain_events_dispatch.entregar_evento`, extraída del despachador sin
  cambiar su comportamiento.
- ADR-0016: adenda con el canary de un asistente cuyos artefactos viven en `TENANTS_ROOT`: gate en el repo del
  negocio con k = 8 y rollback por revert más reaplicar.
