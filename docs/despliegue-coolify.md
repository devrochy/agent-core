# Despliegue en Coolify

Runbook del despliegue de agent-core en Coolify (misma máquina de desarrollo,
igual que agent-sale). Un solo entorno por ahora, siguiendo la rama `develop`.

| Recurso        | Nombre           | UUID                    | Detalles |
| -------------- | ---------------- | ----------------------- | -------- |
| Proyecto       | Agent Core       | `xqrxaj7by6yclupym5u9rcvj` | env `production` (`mihlppg36bf846gpu7zfu2h3`) |
| Postgres       | agent-core-postgres | `huvhzaxim2notqkcxqa5930a` | `pgvector/pgvector:pg17`, db `agent_core`, port loopback `127.0.0.1:5439:5432` |
| Redis          | agent-core-redis | `gk9bagcojtdrtofcx8tdxkiq` | `redis:7.2`, port loopback `127.0.0.1:6381:6379` |
| App            | agent-core-test  | `ggzjdyc7zsemt6kzafx5rg7m` | source GitHub `devrochy/agent-core` rama `develop`, build pack `Dockerfile`, puerto contenedor `8000`, ports `127.0.0.1:3003:8000`, health `/healthz` |

## Dominio público

agent-core (test) se sirve en **`https://app.solvershub.site`** vía **Cloudflare
Tunnel** (outbound: no se abren puertos ni se gestiona TLS). `solvershub.site`
está en Cloudflare; el tunnel publica:

```
https://app.solvershub.site → http://localhost:3003  (agent-core test)
```

- El conector **`cloudflared`** corre como **recurso Docker en Coolify** con el
  `TUNNEL_TOKEN` del tunnel y `--network host` para alcanzar `http://localhost:3003`.
- El **Public Hostname** del tunnel (`app.solvershub.site` → `http://localhost:3003`)
  se configura en Zero Trust → Networks → Tunnels; eso crea el DNS (CNAME).
- **Cloudflare Access**: aplicación para `app.solvershub.site` con policy *Allow*;
  **Bypass** para `/webhooks/*` y `/healthz` (Meta y el health check no autentican).
- El host dedicado sirve la app en la **raíz**: `ROOT_PATH` queda **vacío** y el
  panel funciona en `https://app.solvershub.site/admin`.

> Reemplaza a la Funnel de Tailscale (`https://devrochy.tail2ad60a.ts.net/agent-core`),
> que se degradaba de forma intermitente (el TLS público se cortaba y Meta dejaba
> de entregar webhooks) y requería `sudo systemctl restart tailscaled`.

## Variables de entorno (App, por API/UI de Coolify)

| Variable | Valor |
| --- | --- |
| `APP_ENV` | `production` (**no disponible en build**) |
| `PUBLIC_BASE_URL` | `https://app.solvershub.site` |
| `ROOT_PATH` | *(vacío)* — host dedicado, la app se sirve en la raíz |
| `DATABASE_URL` | `postgresql+asyncpg://app_runtime:app_runtime@huvhzaxim2notqkcxqa5930a:5432/agent_core` |
| `MIGRATIONS_DATABASE_URL` | `postgresql+asyncpg://agent_core_migrations:<pw>@huvhzaxim2notqkcxqa5930a:5432/agent_core` |
| `REDIS_URL` | `redis://default:<pw>@gk9bagcojtdrtofcx8tdxkiq:6379/0` |
| `MASTER_ENCRYPTION_KEY` | Fernet, propia del entorno |
| `APP_SECRET_KEY` | hex aleatorio |
| `LLM_PROVIDER` | `deepseek` |
| `LLM_API_KEY` | clave del proveedor |
| `LLM_MODEL` | `deepseek-chat` |
| `DEMO_MODE` | `false` |
| `LOG_LEVEL` | `info` |
| `AGENT_CORE_ROLE` | `api` |

Notas:

- **Roles de Postgres:** el runtime conecta como `app_runtime` (lo crea la
  migración `0003`, password `app_runtime`); las migraciones corren como
  `agent_core_migrations` (SUPERUSER, creado a mano). Así RLS se aplica de
  verdad y la password maestra de Coolify no entra en el entorno de la app.
- **`APP_ENV` va "Not available during build"** (mismo gotcha que agent-sale):
  las variables de la app se inyectan también en el build.
- **`PUBLIC_BASE_URL` es la URL pública del host**: de ahí salen el webhook del
  tenant y los OAuth de packs.

## Despliegue

**La imagen hornea los packs** (`packages/pack-*` y `plugins/`) y el Dockerfile
declara `ENTRYPOINT ["/app/scripts/entrypoint.sh"]`: en Coolify no hay
bind-mounts como en compose, y sin el entrypoint las migraciones no corren (el
arranque moría con `password authentication failed for user "app_runtime"`).

- Migraciones y plugins corren en el **arranque del contenedor** (entrypoint),
  no como comando pre-deployment de Coolify (que corre dentro de la imagen
  vieja). Pre/post-deployment configurados como `echo skip`.
- El entrypoint hace `mkdir -p plugins` antes de `flock`: en la imagen de
  Coolify el dir `plugins/` no existe (git no trackea dirs vacíos y el
  `.install.lock` está gitignoreado).

Para desplegar por API (token de la sección "Operar por API"):

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{}' \
  "http://127.0.0.1:8000/api/v1/applications/ggzjdyc7zsemt6kzafx5rg7m/start"
```

> El deploy de una app en Coolify es `POST /applications/{uuid}/start`
> (`action_deploy`), no `/deploy`.

## Onboarding de un tenant (E2E)

El alta puede hacerse desde el panel (`https://app.solvershub.site/admin/tenants`),
o con el CLI dentro del contenedor (usa la BD desplegada y valida contra la Graph API):

```bash
APP=$(docker ps --format '{{.Names}}' | grep ggzjdyc | head -1)
docker exec "$APP" agent-core tenant create --non-interactive \
  --name "E2E Test" --slug e2e --business "Tenant de prueba" \
  --wa-label "WhatsApp E2E" \
  --wa-phone-number-id <phone_number_id> \
  --wa-app-secret <app_secret> \
  --wa-access-token <access_token> \
  --wa-verify-token <verify_token>
```

Luego en Meta (App → WhatsApp → Configuration):

- Callback URL: `https://app.solvershub.site/webhooks/whatsapp/<slug>`
- Verify token: el de la conexión del tenant
- Campo `messages`

> **Access token de Meta en dev caduca cada 24 h.** Regenerarlo en la app de
> Meta antes de cada prueba y volver a cargarlo (CLI o panel).

## Verificación post-despliegue

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep ggzjdyc        # Up (healthy)
curl -s http://127.0.0.1:3003/healthz                              # {"status":"ok"}
curl -s https://app.solvershub.site/healthz                        # desde internet (Cloudflare)
curl -s "https://app.solvershub.site/webhooks/whatsapp/<slug>?hub.mode=subscribe&hub.verify_token=<vt>&hub.challenge=x"  # → x
# migraciones al head:
docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -d agent_core -tAc "SELECT version_num FROM alembic_version;"   # 0038
# el rol de runtime existe:
docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -d agent_core -tAc "SELECT rolname FROM pg_roles WHERE rolname='app_runtime';"
```

## Gotchas conocidos

- **El panel requiere un host en la raíz.** Con host dedicado (`app.solvershub.site`)
  funciona directo; si se sirve bajo un prefijo de ruta, usar `ROOT_PATH` (ADR-0073).
- **Puertos por defecto de Coolify chocan con agent-sale:** las apps/databases
  nuevas traen `3000:xxxx` de port mapping; cambiarlo a loopback con puerto
  libre (`127.0.0.1:5439`, `127.0.0.1:6381`, `127.0.0.1:3003`) antes del primer
  start.
- **Un contenedor atascado en crash-loop bloquea el siguiente deploy**
  (`No such container` al limpiar). `docker rm -f <container>` y redesplegar.
- **El webhook de Meta exige 443**: lo sirve Cloudflare (TLS en el edge) y el
  tunnel reenvía a `http://localhost:3003`. No hace falta abrir puertos en el host.
- **Los valores de env de Coolify van cifrados** en su BD (Laravel): no
  insertarlos por SQL, usar la UI o la API.

## Operar por API (token)

El API de Coolify arranca deshabilitado. Se activa en
`/settings/advanced` → API access (o `UPDATE instance_settings SET
is_api_enabled=true WHERE id=0;`). Los tokens se guardan con hash (Sanctum);
para el formato exacto ver `personal_access_tokens` (token = sha256 del
plaintext, y se envía `Authorization: Bearer <id>|<plaintext>`).