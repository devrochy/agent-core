## Test de `/admin/collaborators/existe` al día con `eliminada` (2026-11-15)

- ADR-0198 sumó `eliminada` a `ExisteResponse` y `test_existe_y_username_disponible` seguía esperando solo `existe`: dejaba rojo el CI de `develop` (el PR solo corre `-m aislamiento`). La aserción ahora incluye `eliminada: False`. Sin cambios de código de producción.
