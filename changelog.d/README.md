# Fragmentos del CHANGELOG

Cada PR agrega **su propio archivo** aquí en vez de editar `CHANGELOG.md`. Así dos PRs
en paralelo nunca tocan el mismo archivo y no chocan.

- Nombre: `AAAA-MM-DD-<rama-o-tema>.md` (p. ej. `2026-10-08-hu-sh-01-preset-suscripciones.md`).
- Contenido: el mismo formato de siempre, con un encabezado `##` y viñetas:

  ```markdown
  ## Asistente de SolversHub reproducible (2026-10-08)

  - HU-SH-01. Preset genérico `suscripciones`…
  ```

Para ver lo pendiente: `python scripts/changelog.py`. En la release (skill `release`),
`python scripts/changelog.py --version vX.Y.Z --titulo "<título>"` junta los fragmentos y
las secciones "Sin versión" en `## vX.Y.Z — <título> (fecha)` y borra los fragmentos.
