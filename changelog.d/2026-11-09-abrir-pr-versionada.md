## La skill `abrir-pr` queda versionada y su verificador es obligatorio (2026-11-09)

- **`abrir-pr` y la plantilla de PR entran al repo**: antes solo existían en un checkout, así que los worktrees no las tenían. Claude ya puede usar la skill cuando se le pide abrir un PR; el OK antes de publicar se mantiene.
- **`gh pr create` hacia `develop` corre `verificar_pr.py`** desde el hook `guards.py` y se bloquea si hay errores: falta el fragmento de changelog, chocan números de migración o ADR, hay más de una cabeza de Alembic o el árbol no está limpio. Los PR a `main`, los apilados y los de otros repos no se revisan.
