## Runbook: arrancar producción sobre una base nueva y vacía (2026-11-18)

- `docs/runbooks/arranque-base-nueva.md`: base nueva al lado de la vieja en la misma instancia, roles idempotentes (ya existen en el clúster), `GRANT SET ON PARAMETER` de la `0072`, extensiones por base, marca propia (no la de `vaciar_base_nueva.sql`), migración y `sync-packs` desde el arranque de la API, master único, purga de `tenant-1` de la `0003` y verificaciones. Ensayado en Postgres efímero con develop `e085a923`.
