## El CHANGELOG se arma con fragmentos (2026-10-08)

- Cada PR agrega su entrada en un archivo propio `changelog.d/AAAA-MM-DD-<tema>.md` en vez de editar la cabeza de
  `CHANGELOG.md`, que hacía chocar a casi todos los PRs abiertos en paralelo. La release los consolida con
  `python scripts/changelog.py --version vX.Y.Z --titulo "…"` (también toma las secciones "Sin versión" que quedan).
