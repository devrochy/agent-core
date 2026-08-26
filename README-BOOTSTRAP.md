# Kit de arranque para agent-core

Copia el contenido de esta carpeta en la raíz del repositorio `agent-core`.

```bash
cp -r agent-core-bootstrap/. /ruta/a/agent-core/
cd /ruta/a/agent-core
chmod +x .claude/hooks/*.sh
git add -A && git commit -m "chore: bootstrap del devkit de agent-core"
```

## Qué incluye

| Ruta | Qué es |
|---|---|
| `CLAUDE.md` | Invariantes del framework. Siempre en contexto. |
| `.claude/skills/` | 9 skills: procedimientos que se cargan bajo demanda. |
| `.claude/agents/` | 4 subagentes con contexto aislado. |
| `.claude/hooks/` | Reglas deterministas que el modelo no puede saltarse. |
| `.claude/settings.json` | Permisos y registro de hooks. |
| `docs/adr/` | Decisiones de arquitectura. Las 3 primeras ya escritas. |
| `docs/ROADMAP.md` | Sprints con criterio de salida. Es tu tablero. |
| `docs/inventory.md` | Plantilla del inventario del Paso 1. |
| `docs/security.md` | Esqueleto del one-pager de seguridad para ventas. |
| `packages/contracts/src/index.ts` | Los 5 contratos del framework. |
| `prompts/` | Los prompts exactos para pegar en modo plan. |

## Orden de ejecución

1. Copia los ficheros y verifica que los hooks funcionan:
   pide a Claude que cree un `.sql` sin RLS — debe bloquearse.
2. Abre Claude Code, entra en **modo plan** (Shift+Tab dos veces) y pega
   `prompts/00-inventario.md`.
3. Revisa el `docs/inventory.md` que produzca. Corrígelo a mano donde no estés de acuerdo:
   ese fichero manda sobre todo lo demás.
4. Pega `prompts/01-sprint-0.md`. Aprueba el plan. Ejecuta.
5. Al terminar cada sprint: `/release`, tag de git, y actualiza `docs/ROADMAP.md`.

## Reglas de sesión

- **Una fase por sesión.** `/clear` entre fases.
- El estado vive en `docs/`, no en la ventana de contexto.
- Si Claude propone algo que contradice `CLAUDE.md`, es un bug del plan: corrígelo
  antes de aprobar, no después de ejecutar.
