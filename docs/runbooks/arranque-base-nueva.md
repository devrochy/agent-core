# Runbook: arrancar producción sobre una base nueva y vacía

Crea, en la instancia de Postgres de producción que ya existe, una **base nueva al lado de la
vieja**, la migra a `head` desde cero y la deja lista para que la API, el worker y el beat
arranquen sobre ella con **un solo usuario master y sin negocios** (decisión del dueño del
2026-11-18: sin siembra; los negocios se crean después desde el panel). La base vieja no se toca
y queda como vuelta atrás unos 7 días.

- **Quién**: el dueño (u operador con SSH a la máquina de producción). **Ningún paso se corre
  desde una sesión de asistente**: tocan producción.
- **De dónde sale**: [`produccion-vps.md`](produccion-vps.md) §4.3 (roles, extensiones, dueño y
  marca) y §5.3 (aislamiento), y los «Gotchas conocidos» de
  [`../despliegue-coolify.md`](../despliegue-coolify.md). Ese runbook supone un Postgres **vacío y
  propio**; este, una instancia **compartida con la base vieja**, y eso cambia tres cosas: los
  roles ya existen (son del clúster, no de la base), las extensiones se crean en la base nueva
  (son por base) y el `COMMENT ON DATABASE` no puede ser el que habilita
  `scripts/vaciar_base_nueva.sql`.
- **Qué no cubre**: las variables de Coolify y el orden del despliegue (fase F del plan del salto,
  `despliegue-coolify.md`), el respaldo de la base vieja (fase A) y el go/no-go (fase G).
- **Ensayado** en un Postgres efímero el 2026-11-18 con develop `e085a923`: ver
  [Registro del ensayo](#registro-del-ensayo).

Marcadores:

| Marcador | Qué es |
| --- | --- |
| `<uuid-postgres>` | contenedor de Postgres de producción (hoy `huvhzaxim2notqkcxqa5930a`) |
| `<base-nueva>` | nombre de la base nueva. Propuesta: `agent_core_prod`. Solo minúsculas, dígitos y `_`. No puede ser `agent_core` (la vieja) ni empezar por `agentcore_eval` |
| `<correo-master>` | correo del primer usuario master |
| `<uuid-api>`, `<uuid-worker>`, `<uuid-beat>` | UUID de los recursos en Coolify (hoy `ggzjdyc7…`, `wv6kycje…`, `hwh7s5gs…`) |

## 0. Antes de empezar

- [ ] Respaldo fresco de la base vieja hecho y restauración ensayada (`ENSAYO OK`, fase A).
- [ ] La imagen que se va a desplegar es la del commit ensayado o posterior, con la migración
      `0072` o más nueva.
- [ ] Gestor de contraseñas a mano: aquí se generan o se confirman dos claves de base de datos y
      la del master.
- [ ] Los recursos API, worker y beat de producción **no** apuntan todavía a `<base-nueva>`: la
      base nace antes del despliegue.

## 1. Estado de los roles (solo lectura)

```bash
docker exec -it <uuid-postgres> psql -U postgres -d postgres
```

```sql
SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin
FROM pg_roles WHERE rolname IN ('agent_core_migrations', 'app_runtime') ORDER BY 1;
\l
```

Anotar el resultado. En la producción actual los dos roles **ya existen** (los usa la base vieja)
y `agent_core_migrations` es superusuario (medido el 2026-10-25). Lo que se espera:

| Rol | Debe ser | Si no |
| --- | --- | --- |
| `app_runtime` | `rolsuper = f`, `rolbypassrls = f` | paso 2 lo corrige (no rompe la base vieja: el runtime nunca necesitó saltarse RLS) |
| `agent_core_migrations` | `rolbypassrls = t` **o** `rolsuper = t` | paso 2 lo crea si falta |

**Decisión del dueño, no de este runbook:** quitarle el superusuario a `agent_core_migrations`
(`ALTER ROLE agent_core_migrations NOSUPERUSER BYPASSRLS`). Las migraciones de este commit corren
sin superusuario (ensayado, ver el registro), pero el rol también es dueño de la base vieja y la
vuelta atrás usa el código de `main`, que no se ensayó así. Recomendado: dejarlo como está para el
salto y bajarlo cuando se borre la base vieja.

## 2. Roles (idempotente)

En la misma sesión de `psql`, como `postgres`:

```sql
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_core_migrations') THEN
    -- Dueño de las tablas: corre las migraciones. BYPASSRLS porque la 0034 y la 0052 lo exigen.
    CREATE ROLE agent_core_migrations LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE;
    RAISE NOTICE 'agent_core_migrations creado: ponerle clave con \password';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
    -- Runtime (API, worker y beat). Creado ANTES de migrar: si no existe, la 0003 lo crea con
    -- la clave de desarrollo, que está en el repo.
    CREATE ROLE app_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    RAISE NOTICE 'app_runtime creado: ponerle clave con \password';
  END IF;
END $$;
-- Vale también si el rol ya existía: el runtime nunca se salta RLS.
ALTER ROLE app_runtime NOSUPERUSER NOBYPASSRLS;
```

**Claves.** Si el paso creó un rol (aparece el `NOTICE`), ponerle una clave nueva:
`\password app_runtime` o `\password agent_core_migrations` (la pide sin dejarla en el historial;
al gestor de contraseñas).

Si el rol ya existía, **no** cambiarle la clave salvo que sea la de desarrollo: la base vieja la
usa y la vuelta atrás consiste en volver a apuntar `DATABASE_URL` a ella con esa misma clave.
Para saber si `app_runtime` tiene la de desarrollo (la que pone la `0003`), mirar la clave de la
`DATABASE_URL` actual de la API de producción en Coolify: si es `app_runtime`, cambiarla con
`\password app_runtime` y anotar que la URL de vuelta atrás (base vieja) lleva la clave nueva. Ojo: la
API, el worker y el beat de hoy usan ese mismo rol contra la base vieja, así que en cuanto cambie
la clave sus conexiones nuevas fallan. Se hace **dentro de la ventana**, no antes.
`verificar_aislamiento.py` (paso 7.3) lo vuelve a comprobar sobre la URL real.

> No sirve probar la clave con `psql -h 127.0.0.1` dentro del contenedor de Postgres: la imagen
> confía (`trust`) en las conexiones locales y entra con cualquier clave (visto en el ensayo).

## 3. Permiso de la `0072`

```sql
-- purge_tenant (borrado de un negocio) apaga los triggers con session_replication_role, que es
-- de superusuario: la 0072 aborta sin este permiso. Vale en todo el clúster. Nunca a app_runtime.
GRANT SET ON PARAMETER session_replication_role TO agent_core_migrations;
SELECT rolsuper OR has_parameter_privilege('agent_core_migrations', 'session_replication_role', 'SET')
FROM pg_roles WHERE rolname = 'agent_core_migrations';   -- tiene que dar t
```

Si `agent_core_migrations` es superusuario el `GRANT` no hace falta, pero no estorba y deja la
base lista para el día en que se le quite.

## 4. Base nueva, marca y extensiones

```sql
\set base <base-nueva>
CREATE DATABASE :"base" OWNER agent_core_migrations;
-- Marca de identificación. NO es 'solvershub-produccion-vps': esa es la que habilita
-- scripts/vaciar_base_nueva.sql, y esta base va a tener los datos reales desde el primer día.
COMMENT ON DATABASE :"base" IS 'solvershub-produccion-base-nueva-2026-11';
\c :base
-- Son por base (no por clúster). vector y postgis exigen superusuario; las migraciones usan
-- IF NOT EXISTS.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;
SELECT extname, extversion FROM pg_extension ORDER BY 1;
SELECT current_database(), pg_get_userbyid(datdba), shobj_description(oid, 'pg_database')
FROM pg_database WHERE datname = current_database();
\q
```

Esperado: `btree_gist`, `plpgsql`, `postgis` y `vector`; dueño `agent_core_migrations`; la marca.
La base vieja (`agent_core`) sigue igual: `\l` del paso 1 contra `\l` ahora solo agrega una fila.

Con las claves se arman las URL de la base nueva (si una clave tiene `@ : / ? # %`, va
codificada):

- `MIGRATIONS_DATABASE_URL=postgresql+asyncpg://agent_core_migrations:<clave>@<host>:5432/<base-nueva>` (solo la API)
- `DATABASE_URL=postgresql+asyncpg://app_runtime:<clave>@<host>:5432/<base-nueva>` (API, worker y beat)

`<host>` es el mismo de las URL actuales de producción; solo cambia la base al final.

## 5. Migrar a `head` y registrar las habilidades

El contenedor de la API cambia de nombre en cada deploy; en los pasos que siguen:

```bash
API=$(docker ps --format '{{.Names}}' | grep <uuid-api> | head -1)
```

Lo hace el **arranque de la API** (`scripts/entrypoint.sh`: `agent-core db migrate` y después
`agent-core db sync-packs`). Se cargan las dos URL del paso 4 en la API (`DATABASE_URL` también en
worker y beat) y se despliega **solo la API**. En el log:

- `[entrypoint] aplicando migraciones (core + plugins)...`
- `✓ Migraciones del core aplicadas` y una línea `✓ <pack>: migraciones aplicadas` por pack.
- `[entrypoint] registrando versiones de habilidades...` seguido de `inserted: 6` (una por pack con
  habilidades) y **sin** `AVISO: sync-packs reportó problemas`.
- El contenedor queda `healthy`.

Si se quiere migrar antes de desplegar (o repetir), con la imagen nueva ya construida y las dos URL
en el entorno del contenedor:

```bash
docker exec "$API" agent-core db migrate
docker exec "$API" agent-core db sync-packs
```

Las dos son idempotentes: `db migrate` otra vez no imprime ninguna línea `Running upgrade`, y
`sync-packs` otra vez da `exists: 6`.

**No** correr `scripts/init_db.py` ni `agent-core db seed`: crean el admin de desarrollo.

## 6. Primer master (y nada más)

Con la API arriba sobre la base nueva:

```bash
docker exec -it "$API" agent-core admin create --email <correo-master> --role master
```

Sin `--password`, el comando genera una clave y la muestra una vez: al gestor de contraseñas. (Con
`--password` quedaría en el historial del shell.) `admin grant` **no** hace falta: el rol `master`
se salta los permisos de sección. Comprobación:

```bash
docker exec "$API" agent-core admin list
```

Una sola fila: `<correo-master>`, rol `master`.

### 6.1 El negocio `tenant-1` que crea la `0003`

La migración `0003` siembra un negocio `tenant-1` («Tenant #1») en cualquier base nueva, y las
`0127`/`0128` cuentan con él. Para que la base quede **sin negocios**, se suspende y se purga.
Es irreversible, pero en una base recién creada no hay nada que perder, y de paso prueba de punta a
punta el permiso del paso 3 (`purge_tenant` es la función de la `0072`):

```bash
docker exec "$API" agent-core tenant deactivate tenant-1
docker exec "$API" agent-core tenant purge tenant-1 --confirm "ELIMINAR tenant-1"
docker exec "$API" agent-core tenant list   # vacío
```

Esperado: `✓ tenant-1 eliminado por completo (irreversible)`. Si `purge` falla con
`session_replication_role`, falta el paso 3.

## 7. Verificar

### 7.1 Versión de las migraciones

```bash
docker exec "$API" agent-core db migrate 2>&1 | grep -c 'Running upgrade'   # 0
docker exec <uuid-postgres> psql -U postgres -d <base-nueva> -tAc \
  "select 'core', version_num from alembic_version"
docker exec <uuid-postgres> psql -U postgres -d <base-nueva> -tAc \
  "select tablename from pg_tables where schemaname='public' and tablename like '%alembic_version%' order by 1"
```

La versión del core tiene que ser la última de `alembic/versions` del commit desplegado. Cada pack
tiene su propia tabla de versiones (`alembic_version_<pack>`): leerlas con el mismo
`select version_num` y compararlas con la última revisión de `packages/pack-*/alembic/versions`.
En el commit ensayado (`e085a923`):

| Tabla | Versión |
| --- | --- |
| `alembic_version` (core) | `0147` |
| `alembic_version_pack_calendar` | `0006` |
| `alembic_version_pack_cobranza` | `pack_cobranza_0019` |
| `alembic_version_pack_commerce` | `crm_0020` |
| `alembic_version_pack_knowledge` | `pack_knowledge_0002` |
| `alembic_version_pack_payments` | `pack_payments_0004` |
| `alembic_version_pack_subscriptions` | `pack_subscriptions_0014` |

`pack-voice` no tiene migraciones.

### 7.2 RLS forzada sin huecos

```bash
docker exec <uuid-postgres> psql -U postgres -d <base-nueva> -tAc "
  select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where c.relkind in ('r','p') and n.nspname not in ('pg_catalog','information_schema')
    and c.relrowsecurity and not c.relforcerowsecurity"
```

Con el commit ensayado da `1`: `meta_private.meta_tokens`, el pendiente conocido de 7.3. Fuera de
esa tabla tiene que dar `0` (en `public`, `rls_forced` de 7.3 lo vuelve a mirar).

### 7.3 Aislamiento (criterio de `produccion-vps.md` §5.3)

```bash
docker exec "$API" python scripts/verificar_aislamiento.py | tee aislamiento-base-nueva.txt
```

Guardar la salida con fecha. Con el commit ensayado sale **código 1**, y es esperado:

```text
[OK] rls_forced: RLS forzada en todo public, meta_private sin grants a PUBLIC
[X ] rls_otros_esquemas: RLS sin FORCE fuera de public: meta_private.meta_tokens
[OK] rol_runtime: app_runtime no es superusuario, no tiene BYPASSRLS y no es dueño de ninguna base, esquema ni tabla
[OK] rol_esperado: Conecta como app_runtime
[OK] contrasena_runtime: No es la contraseña de desarrollo
```

`meta_private.meta_tokens` nace de la `0018` con RLS habilitada y sin `FORCE`, y la `0029` solo
forzó `public`: es el pendiente conocido de `docs/BACKLOG.md` y de la fase 0 de
`produccion-vps.md`, sin migración que lo arregle todavía. No hay fuga (el runtime no es dueño,
así que RLS le aplica) y producción sale sin Meta, así que la tabla queda vacía. Cualquier otra
línea en `[X ]` frena el salto. Cuando exista la migración que fuerza RLS en esa tabla, el
esperado pasa a ser todo `[OK]` y código 0.

### 7.4 Arranque y login

- `curl -fsS http://127.0.0.1:<puerto-api>/healthz` → `200`.
- Login del master contra la API (`POST /api/v1/auth/login` con `identifier` y `password`) → `200`
  con `token`; o entrar al panel con `<correo-master>`.
- Worker y beat desplegados después de la API (mismo commit): `celery@… ready` y
  `beat: Starting...`; a los dos minutos, `docker exec "$API" python scripts/verify_production.py`
  con `[OK] worker_heartbeat_and_dlq`.
- En los logs de los tres: **cero** `plugin_activation_failed`, cero `cron_task_failed` y, en el
  beat, cero `cron_scheduler_lectura_fallida`. Un aviso `inbound_trim_blocked_by_pending` con
  `corte_id: 0-1` es esperado mientras no entre el primer mensaje (el grupo del stream de entrada
  está en `0-0`): no frena nada.

```bash
for u in <uuid-api> <uuid-worker> <uuid-beat>; do
  docker logs "$(docker ps --format '{{.Names}}' | grep "$u" | head -1)" 2>&1 \
    | grep -cE 'plugin_activation_failed|cron_task_failed|cron_scheduler_lectura_fallida'
done   # 0, 0 y 0
```

## Vuelta atrás

Mientras no haya datos reales en la base nueva: volver `DATABASE_URL` (y `MIGRATIONS_DATABASE_URL`)
de los tres recursos a la base vieja, con las claves de siempre (o la nueva de `app_runtime`, si
el paso 2 la cambió), y los recursos al commit anterior. La base nueva puede quedarse o borrarse
(`DROP DATABASE <base-nueva>`, con OK). Nunca `alembic downgrade`.

## Registro del ensayo

**2026-11-18**, develop `e085a923` (core `0147`), en la máquina de desarrollo, sin tocar test ni
producción:

- **Postgres**: contenedor efímero de `ghcr.io/devrochy/solvershub-postgres:pg17-postgis`
  (PG 17.11, postgis 3.6.4, vector 0.8.7), la misma imagen de producción y del CI. Redis `redis:7.2`.
  Puertos dinámicos en `127.0.0.1`.
- **Estado de partida simulado**: una base `agent_core` con una fila de marca (la «vieja») y los
  dos roles **ya creados**, como en producción: `agent_core_migrations` `NOSUPERUSER BYPASSRLS` y
  `app_runtime` con la clave de desarrollo. Se usó el dueño **sin** superusuario a propósito: en
  test las migraciones corren como `postgres`, así que la `0072` a la `0147` y las de los packs no
  se habían corrido nunca así. En producción el rol es superusuario, el caso fácil.
- **Entorno**: `APP_ENV=production`, `APP_SECRET_KEY`, `MASTER_ENCRYPTION_KEY` y
  `TOKEN_MASTER_KEYS` nuevas generadas para el ensayo, `TRUSTED_PROXY_CIDRS=127.0.0.1/32`, sin
  `QA_TESTERS_ENABLED`, sin `META_*`, sin `.env`. API con `uvicorn core.main:app`, worker con
  `celery worker -P solo` (el Python local no trae `_ctypes` para prefork) y beat con
  `-S core.tasks.cron_scheduler:DbOverrideScheduler`, como `entrypoint-beat.sh`.

| Paso | Resultado |
| --- | --- |
| 1. Estado de roles | los dos roles listados con sus atributos |
| 2. Roles | el bloque `DO` no hace nada con roles existentes y no falla; `ALTER ROLE app_runtime` sin error. Clave de `app_runtime` cambiada (era la de desarrollo). Probar la clave con `psql -h 127.0.0.1` dentro del contenedor dio falso positivo (`trust`): se cambió el método del paso 2 |
| 3. `GRANT SET ON PARAMETER` | `GRANT`; comprobación `t` |
| 4. Base, marca y extensiones | `agent_core_prod` con dueño `agent_core_migrations`, marca puesta, cuatro extensiones. La base vieja intacta (la fila de marca sigue) |
| 5. `db migrate` | core `0001→0147` y los 6 packs a su última revisión en 9 s, con el dueño **sin** superusuario; 192 tablas de `public` del dueño (solo `spatial_ref_sys` de `postgres`). Repetido: 0 `Running upgrade` |
| 5. `sync-packs` | `inserted: 6`, `errors=0`; repetido, `exists: 6` |
| 6. Master | `admin create --role master` sin `--password`: creado; `admin list` con una sola fila |
| 6.1 `tenant-1` | estaba (`active`, `pack_commerce`); `deactivate` y `purge --confirm "ELIMINAR tenant-1"`: `eliminado por completo`; `tenants` con 0 filas. Prueba de punta a punta del permiso de la `0072` sin superusuario |
| 7.1 Versiones | las de la tabla de 7.1 |
| 7.2 RLS sin FORCE | `1`: `meta_private.meta_tokens` (pendiente conocido, ver 7.3). En `public`, 0 |
| 7.3 Aislamiento | `rls_otros_esquemas` en `[X ]` por `meta_tokens`; los otros cuatro en `[OK]` |
| 7.4 `/healthz` | `200 {"status":"ok"}` antes y después de purgar `tenant-1` y reiniciar la API |
| 7.4 Login | `POST /api/v1/auth/login` del master `200` con token; `GET /api/v1/admin/me` `200` rol `master`; clave mala `401` |
| 7.4 Worker y beat | `celery@… ready` y `beat: Starting...`; en 4 minutos, 13 tareas enviadas por el beat y 12 completadas en el worker; `verify_production.py`: `[OK] worker_heartbeat_and_dlq`, `[OK] rls_forced`, `[OK] token_keys_configured` (los de Meta y `PUBLIC_BASE_URL` fallan: no hay borde ni Meta) |
| 7.4 Logs | **0** `plugin_activation_failed`, **0** `cron_task_failed` y **0** `cron_scheduler_lectura_fallida` en los tres. Los 6 packs detectados. Único aviso: `inbound_trim_blocked_by_pending` con Redis nuevo (ver 7.4) |

Hallazgos que no se arreglan en este runbook:

- `meta_private.meta_tokens` sin `FORCE ROW LEVEL SECURITY`: necesita una migración (invariante 1).
- `inbound_trim_blocked_by_pending` salta con un stream recién creado (`last-delivered-id 0-0`):
  es un falso aviso de `core/orchestrator/retencion_streams.py`.
- La `0003` siembra `tenant-1` en toda base nueva: aquí se purga a mano (paso 6.1).
