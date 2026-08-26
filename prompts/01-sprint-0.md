# Prompt 2 — Sprint 0: cimientos (modo plan)

> Requiere `docs/inventory.md` completo y revisado a mano.
> Activa modo plan antes de pegar esto.

---

Lee `CLAUDE.md`, `docs/adr/`, `docs/inventory.md` y `docs/ROADMAP.md`.

Planifica el Sprint 0. Alcance exacto, ni más ni menos:

1. **Monorepo**: pnpm workspaces + Turborepo con la estructura declarada en `CLAUDE.md`.
   Los paquetes se crean vacíos con su `package.json` y su `CLAUDE.md` propio; no se migra
   código todavía.

2. **Contratos**: `packages/contracts` ya trae `src/index.ts`. Verifica que compila,
   añade la configuración de TypeScript del monorepo y etiqueta mentalmente v1.0.0.
   Si detectas que a algún contrato le falta algo esencial, **no lo cambies**: repórtalo
   como propuesta de ADR y espera mi decisión.

3. **ADRs**: separa `docs/adr/0004-0010-borradores.md` en ficheros individuales usando el
   formato del skill `/adr`. Completa contexto, alternativas y condición de revisión de cada uno.

4. **Verificación del devkit**: crea un archivo SQL de prueba en `migrations/` SIN RLS para
   comprobar que el hook lo bloquea. Reporta el resultado y borra el archivo de prueba.

5. **Auditoría inicial**: lanza el subagente `auditor-multitenant` contra el código actual y
   guarda el resultado en `docs/audit/2026-08-inicial.md`.

**Restricciones:**
- No migres lógica de CULTO en este sprint.
- No crees tablas ni migraciones reales.
- No instales dependencias que no sean del tooling del monorepo.

Antes de ejecutar, muéstrame el plan con: archivos a crear, archivos a modificar, y
cualquier punto donde necesites una decisión mía. Si algo del plan contradice un ADR, dilo
en la primera línea.
