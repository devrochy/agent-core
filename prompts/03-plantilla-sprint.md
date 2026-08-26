# Plantilla — cualquier sprint siguiente (modo plan)

Copia, reemplaza `<N>` y ajusta el alcance.

---

Lee `CLAUDE.md`, `docs/adr/`, `docs/ROADMAP.md` y `docs/inventory.md`.

Planifica el Sprint <N> según `docs/ROADMAP.md`.

**Antes de proponer el plan:**
1. Lanza el subagente `arquitecto` con tu propuesta inicial y ajústala según su dictamen.
2. Si algo del sprint requiere una decisión de arquitectura nueva, redáctala como ADR con
   `/adr` y espera mi aprobación antes de implementarla.

**El plan debe incluir:**
- Archivos a crear y a modificar, agrupados por paquete.
- Qué skill del devkit aplica a cada paso (`/new-tool`, `/db-migration`, `/new-pack`).
- Los tests que constituyen el criterio de salida, escritos antes que la implementación.
- Los puntos donde necesitas una decisión mía.

**Al terminar la ejecución:**
1. `pnpm lint && pnpm test` en verde.
2. Subagente `auditor-multitenant`, sin hallazgos de severidad alta.
3. Actualiza `docs/ROADMAP.md`: marca lo hecho, mueve el sprint activo, y registra en la
   tabla de deuda cualquier cosa que se quedó a medias.
4. `/release` para el tag.

**Restricciones permanentes:**
- Nada de código específico de cliente en `packages/core-*`.
- Nada de prompts en archivos `.ts`.
- Refactor y cambio funcional nunca en el mismo commit.
