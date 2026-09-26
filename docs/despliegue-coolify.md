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

agent-core se sirve bajo un **prefijo de ruta en el Funnel 443** de Tailscale,
porque Meta exige HTTPS/443 para el webhook y el 443 ya estaba ocupado por el
test de agent-sale:

```
https://devrochy.tail2ad60a.ts.net/          → 127.0.0.1:3000  (agent-sale test, intacto)
https://devrochy.tail2ad60a.ts.net/agent-core → 127.0.0.1:3003  (agent-core)
```

Tailscale quita el prefijo `/agent-core` al reenviar, así que agent-core recibe
rutas limpias (`/healthz`, `/webhooks/...`). Se montó con:

```bash
sudo tailscale funnel --set-path=/agent-core --bg --https=443 127.0.0.1:3003
tailscale funnel status          # verificar: / y /agent-core en el 443
```

**Panel bajo el prefijo (ADR-0073):** con `ROOT_PATH=/agent-core` la app prefija
las URLs que genera (redirects, links, estáticos) con un middleware, así que el
panel admin **sí funciona** en `https://devrochy.tail2ad60a.ts.net/agent-core/admin`
(y el OAuth de Calendar completa). Sin `ROOT_PATH` (vacío) la app se sirve en la
raíz. El proxy sigue quitando el prefijo al reenviar; el middleware solo reescribe
la salida.

> **Riesgo / pendiente de dominio estable (2026-09-22):** el **DNS público de la
> Funnel de Tailscale se cayó** (`NXDOMAIN` en resolvers públicos) hasta reiniciar
> `tailscaled` → Meta no puede verificar ni entregar mientras tanto. Además, el
> dominio *default* de Coolify para la app es `http://<app>.186.82.102.11.sslip.io`
> (**HTTP, sin TLS**) y los **puertos 80/443 del host están cerrados** desde internet,
> así que Coolify/Traefik no puede servir HTTPS ni validar Let's Encrypt. **Decisión
> pendiente** (ver `docs/BACKLOG.md`): Cloudflare Tunnel (`agentcore.formotos.com`,
> outbound, requiere `cloudflared tunnel login` en la cuenta dueña de `formotos.com`)
> vs abrir 80/443 y usar el dominio de Coolify.

## Variables de entorno (App, por API/UI de Coolify)

| Variable | Valor |
| --- | --- |
| `APP_ENV` | `production` (**no disponible en build**) |
| `PUBLIC_BASE_URL` | `https://devrochy.tail2ad60a.ts.net/agent-core` |
| `ROOT_PATH` | `/agent-core` (prefijo del proxy; ADR-0073) |
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
- **`PUBLIC_BASE_URL` incluye el prefijo** `/agent-core`: de ahí sale el webhook
  del tenant y los OAuth de packs.

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

El panel no sirve bajo el prefijo, así que el alta se hace con el CLI dentro del
contenedor (usa la BD desplegada y valida las credenciales contra la Graph API):

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

- Callback URL: `https://devrochy.tail2ad60a.ts.net/agent-core/webhooks/whatsapp/<slug>`
- Verify token: el de la conexión del tenant
- Campo `messages`

> **Access token de Meta en dev caduca cada 24 h.** Regenerarlo en la app de
> Meta antes de cada prueba y volver a cargarlo (CLI o panel).

## Verificación post-despliegue

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep ggzjdyc        # Up (healthy)
curl -s http://127.0.0.1:3003/healthz                              # {"status":"ok"}
curl -s https://devrochy.tail2ad60a.ts.net/agent-core/healthz      # desde FUERA del tailnet
curl -s "https://devrochy.tail2ad60a.ts.net/agent-core/webhooks/whatsapp/<slug>?hub.mode=subscribe&hub.verify_token=<vt>&hub.challenge=x"  # → x
# migraciones al head:
docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -d agent_core -tAc "SELECT version_num FROM alembic_version;"   # 0011
# el rol de runtime existe:
docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -d agent_core -tAc "SELECT rolname FROM pg_roles WHERE rolname='app_runtime';"
```

## Gotchas conocidos

- **El panel no carga bajo `/agent-core`** (rutas absolutas en las plantillas).
  El webhook y la API no se ven afectados.
- **Puertos por defecto de Coolify chocan con agent-sale:** las apps/databases
  nuevas traen `3000:xxxx` de port mapping; cambiarlo a loopback con puerto
  libre (`127.0.0.1:5439`, `127.0.0.1:6381`, `127.0.0.1:3003`) antes del primer
  start.
- **Un contenedor atascado en crash-loop bloquea el siguiente deploy**
  (`No such container` al limpiar). `docker rm -f <container>` y redesplegar.
- **El webhook de Meta exige 443** y el Funnel tiene un solo hostname: un solo
  prefijo de app a la vez puede tener E2E real (agent-sale en `/`, agent-core
  en `/agent-core`).
- **Los valores de env de Coolify van cifrados** en su BD (Laravel): no
  insertarlos por SQL, usar la UI o la API.

## Operar por API (token)

El API de Coolify arranca deshabilitado. Se activa en
`/settings/advanced` → API access (o `UPDATE instance_settings SET
is_api_enabled=true WHERE id=0;`). Los tokens se guardan con hash (Sanctum);
para el formato exacto ver `personal_access_tokens` (token = sha256 del
plaintext, y se envía `Authorization: Bearer <id>|<plaintext>`).