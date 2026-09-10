---
name: release
description: >
  Publica una versión de un paquete o pack de agent-core con las verificaciones obligatorias.
  Usar SOLO cuando el usuario diga "vamos a desplegar", "release", "publicar la versión" o
  "subir a producción".
---

Release de `$0` con incremento `$1`.

## Checklist (para todo, o no hay release)

1. `pnpm lint && pnpm test --filter $0` en verde.
2. Subagente `auditor-multitenant` sin hallazgos de severidad alta.
3. Subagente `eval-runner` sobre el golden set: sin regresiones frente al release anterior.
4. Migraciones con `.down.sql` probado en staging (aplicar y revertir).
5. Semver correcto:
   - **major** solo si cambia un contrato o el `coreApi` requerido.
   - **minor** si añade tools, entidades o vistas.
   - **patch** para correcciones sin cambio de superficie.
6. `CHANGELOG.md` del paquete actualizado: qué cambió y qué tiene que hacer quien lo usa.
7. Tag de git `<paquete>@vX.Y.Z`.
8. Plan de rollback escrito en el PR. Una línea, pero escrita.

## Despliegue

- Canary al 5% mínimo 24 h antes del 100%.
- Métricas a vigilar: tasa de escalamiento, costo por conversación, latencia p95, errores de tool.
- Cualquiera de esas peor que el baseline → rollback, sin discusión.

## Prohibido

- Editar prompts o AgentSpec directamente en producción. Todo pasa por release versionado.
- Desplegar viernes después de mediodía sin una razón escrita.