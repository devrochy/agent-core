## `purge_tenant` funciona con un dueño sin superusuario (2026-10-09)

- Corrección: el borrado de un negocio (`tenant purge`, panel y API) fallaba siempre en producción.
  `purge_tenant` (migración `0011`) apagaba los triggers con `ALTER TABLE … DISABLE TRIGGER ALL`, que
  en los triggers internos de las FKs exige superusuario ("… is a system trigger"), y el dueño de
  producción es `agent_core_migrations NOSUPERUSER BYPASSRLS`. Hacía rollback completo: sin fuga, pero
  sin borrado. En CI, local y test no se veía porque ahí se migra como superusuario.
- Migración `0072`: `purge_tenant` se declara con `SET session_replication_role = replica` (se
  restaura al salir de la función, también si falla) y `SET search_path = pg_catalog, public,
  pg_temp`, con nombres calificados y descubrimiento de tablas por `pg_catalog`. La fila `tenants` se
  borra con las FKs encendidas: el `ON DELETE SET NULL` de `provisioning_requests.negocio_tenant_id`
  (la solicitud del vendedor) se aplica y una FK inesperada aborta la purga. Siguen igual: SECURITY
  DEFINER, transaccional, master-only y frase `ELIMINAR <slug>`.
- La purga ahora también borra `meta_private.meta_tokens` (tokens cifrados de Meta del negocio), que
  quedaban huérfanos porque solo se miraba el esquema `public`.
- `EXECUTE` de `purge_tenant` solo para `app_runtime` (antes, PUBLIC).
- **Solo se purga un negocio suspendido** (`tenants.status = 'suspended'`, el de `tenant
  deactivate`; no la falta de pago). Lo exigen `core/tenants.py` (`TenantNotSuspendedError`, con un
  mensaje que dice cómo suspenderlo), la CLI (antes de pedir la frase), el panel (el botón
  "Eliminar" solo se habilita en los suspendidos), la API (409 `tenant_not_suspended`) y la función
  SQL (defensa en profundidad: falla sin borrar nada, con `FOR UPDATE` sobre la fila del negocio).
- **Paso manual antes de desplegar** donde el rol de migraciones no sea superusuario (producción):
  como superusuario, `GRANT SET ON PARAMETER session_replication_role TO agent_core_migrations;`.
  Sin eso la `0072` aborta a propósito. Agregado al paso 4.3 de `docs/runbooks/produccion-vps.md`
  y a los gotchas de `docs/despliegue-coolify.md`. Test, CI y el stack local migran como
  superusuario: no necesitan nada.
- Test de integración (`-m aislamiento`): purga con un dueño de la función sin superusuario
  (BYPASSRLS y el permiso sobre el parámetro); comprueba `meta_private`, que otro negocio queda
  intacto, el `SET NULL` de la solicitud del vendedor, el rollback completo sin el permiso y que
  `session_replication_role` no se filtra a la transacción del llamador.
- Tests de integración nuevos: purgar un negocio activo falla en Python y en SQL sin borrar nada
  (ni Redis); la API responde 409 con el negocio activo y 204 una vez suspendido. Los tests de purga
  existentes (frase incorrecta, borrado completo, panel) suspenden antes.
