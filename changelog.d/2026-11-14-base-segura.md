## Check `base-segura` en vez de exigir la rama al día (2026-11-14)

- Nuevo workflow `.github/workflows/base-segura.yml`: publica el estado `base-segura` en
  cada PR. Si el PR va atrasado respecto a su base, solo pide actualizarlo cuando la base
  cambió algo que se cruza con él (mismos archivos, migraciones de Alembic en la misma
  cadena, workflows, `uv.lock` o `pyproject.toml`). Si no, deja mergear sin re-correr el CI.
- Corre en el runner `livianos` (aparte del CI pesado) y se re-evalúa en cada push a
  `develop` y `main`.
