## Runbook de producción en el VPS y verificaciones de la migración (2026-10-09)

- HU-ARCH-01 (preparación; la HU se cierra al ejecutar en el VPS). `docs/despliegue-coolify.md` describe la
  producción en el VPS de dos nodos con Coolify (D-19): topología, recursos, borde con Cloudflare Tunnel y
  Access (solo `/webhooks/*` y `/healthz` son públicos de la API), registros DNS, variables por recurso
  (nombres y origen, sin valores), rotación de secretos, separación de test y observabilidad. La producción
  de la máquina de desarrollo queda como "heredada" hasta el corte.
- `docs/runbooks/produccion-vps.md`: lo que hace el dueño (VPS con dimensionamiento a validar, bucket S3,
  DNS, token del túnel), la instalación de Coolify, los recursos, la base migrada desde cero con el rol
  dueño `agent_core_migrations` (BYPASSRLS, sin superusuario) y el runtime `app_runtime` con clave propia,
  el ensayo, el corte (congelar los webhooks, drenar las colas, `pg_dump` y restauración de solo filas,
  conteos y vuelta atrás), la prueba de apagado y lo que queda para HU-ARCH-02 y HU-ARCH-03.
- `scripts/comparar_conteos.py`: compara el conteo de filas por tabla y las versiones de `alembic_version*`
  entre dos bases (`ORIGEN_DATABASE_URL` y `DESTINO_DATABASE_URL`, solo del entorno). Sale con 1 si algo
  difiere y con 2 si el rol no puede saltarse RLS (contaría 0 en las dos bases y "coincidiría").
- `scripts/verificar_aislamiento.py`: reutiliza el check `rls_forced` del checklist de producción y suma RLS
  sin FORCE fuera de `public`, que el rol del runtime no sea superusuario, no tenga BYPASSRLS ni sea dueño
  de nada, que sea `app_runtime` y que no use la clave de desarrollo. Hoy marca `meta_private.meta_tokens`
  (RLS sin FORCE, ya anotado en `docs/BACKLOG.md`).
- `scripts/vaciar_base_nueva.sql`: deja sin filas la base nueva antes de restaurar; solo corre en la base
  marcada con `COMMENT ON DATABASE … IS 'solvershub-produccion-vps'`, así que no puede vaciar la heredada.
