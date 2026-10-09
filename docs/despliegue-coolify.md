# Despliegue en Coolify

Runbook del despliegue de agent-core y del panel en Coolify (misma máquina de
desarrollo). **Dos entornos** en el proyecto **SolversHub** (`xqrxaj7by6yclupym5u9rcvj`):

| Entorno | Dominio | Rama |
| --- | --- | --- |
| `production` | `https://app.solvershub.site` | `main` (release) |
| `test` | `https://test.solvershub.site` | `develop` |

> A producción se llega por una **release** (`develop` → `main`), no por un merge
> directo: el app de producción despliega la rama `main`.

## Recursos

### Producción — env `production` (`mihlppg36bf846gpu7zfu2h3`)

| Recurso | Nombre | UUID | Detalles |
| --- | --- | --- | --- |
| Postgres | agent-core-postgres | `huvhzaxim2notqkcxqa5930a` | **imagen propia** `127.0.0.1:5000/solvershub-postgres:pg17-postgis` (pgvector + PostGIS), db `agent_core` |
| Redis | agent-core-redis | `gk9bagcojtdrtofcx8tdxkiq` | `redis:7.2` |
| App | agent-core | `ggzjdyc7zsemt6kzafx5rg7m` | `devrochy/agent-core` · `main` · `127.0.0.1:3003:8000` · health `/healthz` |
| Beat | agent-core-beat | `hwh7s5gscnn1k3yqf88xceja` | Celery beat |
| Worker | agent-core-worker | `wv6kycjeae0lmfj11dprkasq` | Celery worker |

### Test — env `test` (`vs22dwtxgnmlfelvkeft3flt`)

| Recurso | Nombre | UUID | Detalles |
| --- | --- | --- | --- |
| Postgres | agent-core-test-postgres | `48h2r2laml64eyrggfg6eqld` | **imagen propia** `127.0.0.1:5000/solvershub-postgres:pg17-postgis` (pgvector + PostGIS), db `agent_core` |
| Redis | agent-core-test-redis | `rqkxxnen7ysp7xmqhocrbidw` | `redis:7.2` |
| App | agent-core-test | `tr0vpdei5av6szcgwpe1vwcg` | `devrochy/agent-core` · `develop` · `3013:8000` · health `/healthz` |
| Panel | panel-web-test | `eyfamlms7ipmba4h6cct2i4a` | `devrochy/solvershub-panel` · `main` · base `/apps/panel-web` · `3014:3000` · health `/login` |
| Beat | clone-of-agent-core-test-beat | `set9kgdxianzvsmprklzyym7` | Celery beat |
| Worker | clone-of-agent-core-test-work | `uqfcdd4a3cf5d4iqa0uosrp0` | Celery worker |

El panel de test llama a la API por **red interna**:
`AGENT_CORE_API_URL=http://172.18.0.1:3013` (gateway del host; el alias por
nombre de contenedor cambia en cada deploy).

## Dominios y túneles (Cloudflare)

`solvershub.site` está en Cloudflare. Hay **dos túneles**:

- **`solvershub`** (`442da5f7-a3fa-4226-9450-2951be30b9d3`): token, config remota
  (dashboard). Sirve **producción**: `app.solvershub.site → http://localhost:3003`.
  El conector corre con `~/.cloudflared/run-solvershub.sh` (crontab `@reboot`).
- **`solvershub-test`** (`88c17ba2-7a02-4300-8a7a-a154d32804df`): **local**
  (`~/.cloudflared/solvershub-test.yml`), sirve **test** con ruteo por path:

  ```
  test.solvershub.site  ^/api/pack/pack_cobranza(/|$) → localhost:3013   (API del pack / APK)
  test.solvershub.site  ^/api/v1(/|$)                 → localhost:3013   (API JSON de la app oficial, SPEC-25)
  test.solvershub.site  ^/webhooks(/|$)               → localhost:3013
  test.solvershub.site  ^/healthz$                    → localhost:3013
  test.solvershub.site  (resto, incl. /api/* del BFF) → localhost:3014   (panel)
  ```

  > La regla `^/api/v1` se agregó el 2026-10-06 para la app oficial (login admin,
  > search, sync, push-token, audit). ⚠️ **Pendiente**: `/api/v1/*` sigue detrás de
  > Cloudflare Access (302 a cloudflareaccess.com), así que la app móvil (sin sesión
  > de Access) NO puede consumirlo por el dominio; hoy se prueba por LAN
  > (`http://192.168.40.15:3013`). Hay que añadir `/api/v1/*` al bypass de Access en
  > test y producción (igual que `/api/pack/*` y `/webhooks`).

  El conector corre con `~/.cloudflared/run-solvershub-test.sh` (crontab `@reboot`).

> El panel y la API **comparten el prefijo `/api`**: por eso el ruteo es fino
> (`/api/pack/pack_cobranza/*` a la API; el resto de `/api/*` al panel). La
> `/api/v1` de la API no se expone: la consume el panel server-side por red interna.

**Access:** `app.solvershub.site` tiene policy *Allow* con bypass en
`/webhooks/*` y `/healthz`.

En **test**, hay una app de Access por hostname con las rutas que necesitan
quedar públicas (la APK no puede hacer SSO):

| Dominio (app de Access) | Policy |
| --- | --- |
| `test.solvershub.site` | **Allow** (emails del equipo) |
| `test.solvershub.site/api/pack/pack_cobranza` | **Bypass** (API de campo / APK) |
| `test.solvershub.site/webhooks` | **Bypass** (Meta) |
| `test.solvershub.site/healthz` | **Bypass** (health check) |

La app raíz protege el panel; el bypass de la API se logra con una Access app
**por path** (Cloudflare evalúa la ruta más específica). Account
`0c0150bffadc10e3f8b01426cfc76ada`, zona `solvershub.site`
`f34c3ab32cc37679cd556455a17e9224`. Para sumar gente al `Allow`, editar la
policy en Zero Trust → Access → Applications.

### Conector de WhatsApp Web (QR)

El canal `waweb` (ADR-0074) necesita un servicio **aparte** del api: sin él, el
panel muestra la tarjeta del QR pero nadie la genera/publica en Redis.

| Entorno | Recurso | UUID | Detalles |
| --- | --- | --- | --- |
| test | agent-core-test-connector | `yqd5gq5j1r1wrgqtjedom16m` | `devrochy/agent-core` · `develop` · mismo Dockerfile |

Configuración clave (Coolify → app → General):
- **Custom Docker Options**: `--entrypoint /app/scripts/entrypoint-connector.sh --tmpfs /tmp/waweb-sessions`
- **Health check**: desactivado (no expone HTTP).
- **Variables**: las mismas del api (copiadas) + `AGENT_CORE_ROLE=connector`,
  `WAWEB_ENABLED=true`, `WAWEB_SESSION_DIR=/tmp/waweb-sessions`.

Notas aprendidas en vivo (2026-09-30):
- **No** agregar `--sysctl net.ipv6.conf.all.disable_ipv6=1`: colgaba a Neonize.
- Neonize (Go) revienta con `concurrent map writes` si se conectan dos sesiones a
  la vez: el conector serializa el puente con un lock global.
- La sesión sin vincular se limpia al iniciar cada ciclo (si no, Neonize
  "reanuda" credenciales viejas y nunca emite QR).
- El watchdog solo recicla sockets que **no** emitieron ningún QR en el ciclo.

## Variables de entorno de la app

| Variable | Producción | Test |
| --- | --- | --- |
| `APP_ENV` | `production` (**no disponible en build**) | `production` |
| `PUBLIC_BASE_URL` | `https://app.solvershub.site` | `https://test.solvershub.site` |
| `ROOT_PATH` | *(vacío)* | *(vacío)* |
| `DATABASE_URL` | `…app_runtime:app_runtime@huvhzaxim2notqkcxqa5930a:5432/agent_core` | `…@48h2r2laml64eyrggfg6eqld:5432/agent_core` |
| `MIGRATIONS_DATABASE_URL` | `…agent_core_migrations:<pw>@huvhzaxim2notqkcxqa5930a:5432/agent_core` | `…postgres:<pw>@48h2r2laml64eyrggfg6eqld:5432/agent_core` |
| `REDIS_URL` | `redis://default:<pw>@gk9bagcojtdrtofcx8tdxkiq:6379/0` | `redis://default:<pw>@rqkxxnen7ysp7xmqhocrbidw:6379/0` |
| `AGENT_CORE_ROLE` | `api` | `api` |
| `PROVISIONING_SELLER_TENANTS` | *(vacío hasta verificar HU-SH-04 en test; después, UUID de `solvershub`)* | UUID de `solvershub` (para verificar HU-SH-03 y HU-SH-04) |
| `BILLING_WOMPI_EVENTOS_TENANT` | UUID de `solvershub` si comparte la cuenta de Wompi con la plataforma; si no, vacío | Igual que producción, con la cuenta del sandbox |
| `BILLING_DIAS_RENOVACION` | `5` (default) | `5` |

Notas:

- **Roles de Postgres:** el runtime conecta como `app_runtime` (lo crea la
  migración `0003`, password `app_runtime`); las migraciones corren como
  superusuario. Así RLS se aplica de verdad.
- **`APP_ENV` va "Not available during build"** (las variables de la app se
  inyectan también en el build).
- Los pares no listados (`MASTER_ENCRYPTION_KEY`, `APP_SECRET_KEY`, `LLM_*`,
  `TOKEN_*`, `WOMPI_*`) son **propios de cada entorno**.
- **`PROVISIONING_SELLER_TENANTS`** (ADR-0121): **UUID** (no slugs), separados por coma, de
  los negocios que venden negocios. Solo un `subscription.activated` de uno de ellos crea un
  negocio nuevo; vacía, el alta automática está apagada. Tiene que estar en el **worker**
  (ahí corre el despachador del outbox); en la app no hace daño. No hay token de servicio
  que configurar. Desde HU-SH-04 (ADR-0123) el plan pago del alta se renueva, el paso de Free a
  pago cambia el plan del negocio y pagar desbloquea en el acto: se puede encender en
  producción **después de verificar HU-SH-04 en test**. Para el correo con el acceso hace falta
  `SYSTEM_SMTP_*`. Altas en revisión: `docs/runbooks/altas-en-revision.md`.
- **`BILLING_WOMPI_EVENTOS_TENANT`** (ADR-0123, riesgo R-05): Wompi admite **una** URL de eventos
  por cuenta y ambiente. Si la plataforma (`WOMPI_BILLING_*`) y el pack de pagos de `solvershub`
  usan la misma cuenta, la URL de eventos de esa cuenta es `https://<host>/webhooks/billing/wompi`
  y esta variable lleva el UUID de `solvershub`: los eventos que no son de un pago de la
  plataforma se le reenvían (su pack los verifica con su secreto). Va en la **app** (recibe el
  webhook). Con cuentas separadas, vacía. Ver `docs/runbooks/cobro-de-plataforma.md`.
- **`BILLING_DIAS_RENOVACION`**: días antes del fin del período en que el job de renovación
  (worker/beat) genera el cobro del período siguiente y avisa por el canal preferido.

## Persistencia de artefactos de tenant (`TENANTS_ROOT`)

Los artefactos por tenant (`flow.json`, `playbooks/`, `catalog.json`, `docs/`) viven
en **`TENANTS_ROOT`** (`/app/tenants`) en el filesystem (ADR-0085). **Sin volumen se
pierden en cada redeploy** — el contenedor de Coolify no los persiste.

- **`docker-compose.yml`**: volumen nombrado **`tenants-data`** montado en
  `/app/tenants` en `core-api`, `celery-worker` y `celery-beat` (los tres leen/escriben
  artefactos). El entrypoint hace `mkdir -p $TENANTS_ROOT`.
- **Coolify**: montar un **volumen persistente en `/app/tenants`** en la app, el worker
  y el beat del entorno (mismo volumen en los tres, o se desincronizan).
- **Backup**: incluir ese volumen (hoy el contenido vive dentro del contenedor).
- Follow-up: mover `flow`/playbooks a BD (versionado) — ver issue de persistencia.

## Despliegue

**La imagen hornea los packs** (`packages/pack-*` y `plugins/`) y el Dockerfile
declara `ENTRYPOINT ["/app/scripts/entrypoint.sh"]`: sin él las migraciones no
corren. Migraciones y plugins corren en el **arranque del contenedor**; los
pre/post-deployment de Coolify están en `echo skip`.

```bash
TOKEN=<api token de Coolify>
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/applications/<uuid>/start"   # deploy de una app
```

## Sembrar datos de demo (test)

El contenedor trae `scripts/seed_demo_login.py` (ADR-0081): crea dos negocios con
`pack_cobranza`, un gestor compartido, un invitado con invitación pendiente y un
master. Todos con contraseña `demo12345`.

```bash
APP=$(docker ps --format '{{.Names}}' | grep tr0vp | head -1)
docker exec "$APP" python scripts/seed_demo_login.py
```

## Verificación post-despliegue

```bash
# producción
docker ps --format '{{.Names}}\t{{.Status}}' | grep ggzjdyc        # Up (healthy)
curl -s https://app.solvershub.site/healthz                        # {"status":"ok"}
# test
curl -s https://test.solvershub.site/healthz                       # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" https://test.solvershub.site/login   # 200
# login unificado (ADR-0081): el gestor demo debe devolver seleccionRequerida
curl -s -X POST https://test.solvershub.site/api/pack/pack_cobranza/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"usuario":"gestor@demo.test","password":"demo12345"}'
# migraciones al head (0042 = tenant_invitations)
docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -d agent_core -tAc "SELECT version_num FROM alembic_version;"
# el rol de runtime existe
docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -d agent_core -tAc "SELECT rolname FROM pg_roles WHERE rolname='app_runtime';"
```

## Gotchas conocidos

- **PostGIS no viene en `pgvector/pgvector`.** El pack-cobranza exige la extensión
  (ADR-0058); la imagen oficial no la trae y las migraciones fallan
  (`Could not open extension control file … postgis`). Se usa una **imagen propia**:
  `docker/postgres/Dockerfile` (`FROM pgvector/pgvector:pg17` + `postgresql-17-postgis-3`).
  Coolify hace `docker compose pull` **en cada arranque**, así que la imagen debe ser
  **pullable** (una imagen solo local no sirve). Se publica en un **registry local**:

  ```bash
  docker run -d --name solvershub-registry --restart always -p 127.0.0.1:5000:5000 registry:2
  docker build -t solvershub-postgres:pg17-postgis docker/postgres
  docker tag solvershub-postgres:pg17-postgis 127.0.0.1:5000/solvershub-postgres:pg17-postgis
  docker push 127.0.0.1:5000/solvershub-postgres:pg17-postgis
  # en Coolify, la DB usa: 127.0.0.1:5000/solvershub-postgres:pg17-postgis
  ```

  Docker trata `127.0.0.1` como registry inseguro permitido (no hace falta tocar
  `daemon.json`). **Dependencia:** el registry debe estar arriba para reiniciar la
  DB; corre con `--restart always`.
- **Puertos de Coolify y loopback.** La API de Coolify **rechaza** el formato
  `127.0.0.1:puerto:puerto` en `ports_mappings` (solo acepta `hostPort:containerPort`,
  que publica en `0.0.0.0`). El loopback se configura en la **UI**, o se cierra
  con firewall (`sudo ufw deny <puerto>`). El túnel entra por `localhost`, así que
  cerrar el puerto no rompe el dominio.
- **App privada:** crear con `POST /api/v1/applications/private-github-app` y
  `github_app_uuid` (la GitHub App instalada); `/applications/public` falla en
  `git ls-remote` con `could not read Username for 'https://github.com'`.
- **Panel:** contexto de build `apps/panel-web` (`base_directory`), Dockerfile
  `/Dockerfile`; healthcheck HTTP a `127.0.0.1:3000/login` (requiere `curl` en la
  imagen, ya incluido). Con `output: "standalone"`.
- **`cloudflared tunnel route dns` por nombre puede fallar** (`Tunnel not found`);
  usar el **UUID** del túnel.
- **Un contenedor atascado en crash-loop bloquea el siguiente deploy**
  (`No such container` al limpiar). `docker rm -f <container>` y redesplegar.
- **El webhook de Meta exige 443**: lo sirve Cloudflare (TLS en el edge).
- **Los valores de env de Coolify van cifrados** en su BD (Laravel): no
  insertarlos por SQL, usar la UI o la API.
- **`POST /applications/{uuid}/restart` no recrea el contenedor**: para que tomen
  efecto cambios de env hay que **redeploy** (`/start`).

## Operar por API (token)

El API de Coolify se activa en `/settings/advanced` → API access. Los tokens se
guardan con hash; se envía `Authorization: Bearer <id>|<plaintext>`.
