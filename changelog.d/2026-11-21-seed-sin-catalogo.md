## `tenant seed` siembra planes sin catalog.json (2026-11-21, HU-AOS-71)

- `agent-core tenant seed <slug>` ya no termina temprano si el negocio no tiene `catalog.json`: siembra igual los `<pack>.json` (p. ej. `planes.json`) y la voz de marca. Sin esto, `solvershub` sin comercio (HU-AOS-71) quedaba sin sus planes en una base nueva. Visto al desplegar en test el 2026-11-21.
