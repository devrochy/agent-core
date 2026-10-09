# Despliegue en Coolify

Cómo se despliegan agent-core (motor/API, worker y beat), el panel (`solvershub-panel`) y la
landing (`solvershub-landing`). Hay **dos entornos y cada uno vive en su propia máquina**
(invariante 9 de `CLAUDE.md`, ADR-0010, decisión D-19):

| Entorno | Dónde corre | Dominios | Rama |
| --- | --- | --- | --- |
| `production` | VPS dedicado de dos nodos (`prod-app` y `prod-data`) con su propio Coolify | `solvershub.site` y `www` (landing), `app.solvershub.site` (panel), `api.solvershub.site` (webhooks) | `main` (release) |
| `test` | máquina de desarrollo, Coolify local | `test.solvershub.site` | `develop` (el panel, `main`) |

> **Estado al 2026-10-09.** La producción en el VPS está definida en este documento y se pone en
> marcha siguiendo [`docs/runbooks/produccion-vps.md`](runbooks/produccion-vps.md) (HU-ARCH-01).
> Mientras no se ejecute el corte, la producción sigue en los recursos de la sección
> [Producción heredada](#producción-heredada-hasta-el-corte), que el runbook apaga y retira. Los
> UUID de los recursos nuevos se anotan aquí al crearlos (fase 11 del runbook).

> A producción se llega por una **release** (`develop` → `main`, skill `release`), nunca por un
> merge directo ni desde una sesión de asistente.

## Producción en el VPS

### Topología

Decisiones: D-19 (VPS dedicado de dos nodos con Coolify y almacenamiento S3 compatible),
D-SPEC-01-1 (Cloudflare Tunnel + Access), D-SPEC-01-2 (Sentry y OTLP) y D-SPEC-01-3 (secretos en
variables de Coolify). Diseño de origen: `solvershub-docs`, SPEC-01 `design.md`.

```
                          Internet (HTTPS; el TLS termina en Cloudflare)
                                        │
             ┌──────────────────────────┴───────────────────────────┐
             │ Cloudflare: DNS · Access · WAF · túnel solvershub-prod │
             └──────────────────────────┬───────────────────────────┘
                                        │ conexión saliente de cloudflared
 ┌──────────────────────────────────────┴─────────────────────────────────────┐
 │ prod-app (Coolify)                                                          │
 │   cloudflared (systemd) ─┬─ 127.0.0.1:3003 → agent-core-api   :8000         │
 │                          ├─ 127.0.0.1:3004 → panel-web        :3000 ─┐      │
 │                          └─ 127.0.0.1:3005 → landing          :3000 ─┤      │
 │   panel y landing llaman a http://agent-core-api:8000 por la red  ◄──┘      │
 │   interna `coolify` (alias de red), nunca por internet                     │
 │   agent-core-worker · agent-core-beat (sin puertos)                         │
 │   volumen agent-core-tenants → /app/tenants en api, worker y beat           │
 └──────────────────────────────────────┬─────────────────────────────────────┘
                                        │ red privada del proveedor
 ┌──────────────────────────────────────┴─────────────────────────────────────┐
 │ prod-data (servidor remoto del mismo Coolify)                              │
 │   agent-core-postgres  Postgres 17 + pgvector + PostGIS   <ip-privada>:5432 │
 │   agent-core-redis     Redis 7.2 con contraseña y AOF     <ip-privada>:6379 │
 └────────────────────────────────────────────────────────────────────────────┘
          bucket S3 compatible en otro proveedor (backups, HU-ARCH-03)
```

- **Ningún puerto entra desde internet** salvo SSH: el firewall del proveedor cierra todo lo demás
  y el tráfico web llega por el túnel, que es una conexión saliente. Docker publica puertos por
  encima de `ufw`, así que `ufw` solo no alcanza (ver [Gotchas](#gotchas-conocidos)).
- **Ningún registro DNS apunta a las IP del VPS**: todo pasa por el túnel.
- `test` sigue en la máquina de desarrollo; producción no comparte con test servidor, Coolify,
  base, Redis, credenciales ni túnel ([Separación de test](#separación-de-test)).

### Recursos

Proyecto **SolversHub**, entorno `production`, en el Coolify de `prod-app`. Los UUID se anotan al
crearlos.

| Recurso (nombre en Coolify) | Nodo | Origen | Arranque | Red y puertos | Salud | Persistencia | UUID |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `agent-core-api` | prod-app | `devrochy/agent-core` · `main` · `Dockerfile` | `ENTRYPOINT` de la imagen: instala packs, **migra** y arranca uvicorn (`Dockerfile:46-47`, `scripts/entrypoint.sh:41-49`) | `127.0.0.1:3003:8000`; alias de red `agent-core-api` | HTTP `/healthz` (`core/main.py:140-142`) | volumen `agent-core-tenants` en `/app/tenants` | `<uuid-api>` |
| `agent-core-worker` | prod-app | el mismo repo, rama y Dockerfile | Custom Docker Options: `--entrypoint /app/scripts/entrypoint-worker.sh` | sin puertos | sin HTTP: latido en Redis cada minuto (`core/tasks/celery_app.py:165-193`) | el mismo volumen | `<uuid-worker>` |
| `agent-core-beat` | prod-app | ídem | `--entrypoint /app/scripts/entrypoint-beat.sh` | sin puertos | igual que el worker | el mismo volumen | `<uuid-beat>` |
| `panel-web` | prod-app | `devrochy/solvershub-panel` · `main` · base `/apps/panel-web` · Dockerfile `/Dockerfile` | `CMD` de la imagen (`node server.js`) | `127.0.0.1:3004:3000` | HTTP `/login` (la imagen trae `curl`) | — | `<uuid-panel>` |
| `landing` | prod-app | `devrochy/solvershub-landing` · `main` · `Dockerfile` | `CMD` de la imagen | `127.0.0.1:3005:3000` | `HEALTHCHECK` de la imagen (`node` contra `/api/health`); el de Coolify, **desactivado** (la imagen no trae `curl` ni `wget`) | — | `<uuid-landing>` |
| `agent-core-postgres` | prod-data | `ghcr.io/devrochy/solvershub-postgres:pg17-postgis`: la imagen de `docker/postgres/Dockerfile` (PG 17 + pgvector + PostGIS, ADR-0058), la misma del CI (ADR-0120) | la de Coolify; superusuario `postgres`, base `agent_core` | `<ip-privada-data>:5432` | la de Coolify | volumen de Coolify | `<uuid-postgres>` |
| `agent-core-redis` | prod-data | `redis:7.2` (la misma que la producción heredada) | con contraseña; AOF activo | `<ip-privada-data>:6379` | la de Coolify | volumen de Coolify | `<uuid-redis>` |
| `cloudflared` | prod-app | paquete oficial, servicio **systemd** (fuera de Coolify, para que el túnel no dependa del panel de Coolify) | `cloudflared service install <token>` | solo saliente | estado del conector en Zero Trust | — | túnel `<uuid-túnel-prod>` |

Equivalencia con `docker-compose.yml` (la topología de desarrollo):

- `postgres` (`docker-compose.yml:11-37`) y `redis` (`:39-47`) → `agent-core-postgres` y
  `agent-core-redis`, en `prod-data`.
- `core-api` (`:49-85`), `celery-worker` (`:87-108`) y `celery-beat` (`:110-129`) → los tres
  recursos de la misma imagen en `prod-app`. Los bind-mounts de compose (`./sdd`, `./packages`,
  `./plugins`) no existen en Coolify: la imagen hornea los packs (`Dockerfile:22-30`). El volumen
  `tenants-data` (`:68-70`, `:155-158`) es `agent-core-tenants`.
- `waweb-connector` (`:133-153`) **no va a producción**: WhatsApp Web no se vende en v1 (P-08) y
  `WAWEB_ENABLED` vale `false` por defecto (`core/config.py:138`).
- `depends_on` (worker y beat esperan a que la API migre, `:104-108`) no existe entre recursos de
  Coolify: el orden lo pone quien despliega. **Primero la API** (aplica las migraciones), y cuando
  está `healthy`, el worker y el beat con el mismo commit.
- El beat corre en **una sola instancia**: dos beats duplican los crons.

El panel y la landing de producción nacen con HU-ARCH-01. La API de campo de cobranza (APK,
`/api/pack/pack_cobranza/*`) y la app oficial (`/api/v1` directo) **no** tienen ruta pública en
producción todavía: entran con HU-ARCH-08 y SPEC-25.

### Borde: Cloudflare Tunnel + Access

Un túnel nuevo, `solvershub-prod`, administrado desde el panel de Zero Trust (token). Su conector
corre en `prod-app`. Rutas publicadas (*Published application routes*); el campo *Path* es una
expresión regular de Go, la misma de las reglas `ingress` de cloudflared, por eso va anclada con
`^`. El túnel las evalúa de arriba hacia abajo: las que tienen path van **antes** que la del mismo
hostname sin path.

| Hostname | Path | Servicio | Para qué |
| --- | --- | --- | --- |
| `api.solvershub.site` | `^/webhooks/` | `http://localhost:3003` | Meta y Wompi (dominio de la API según SPEC-01; HU-ARCH-02 decide si los registros pasan acá) |
| `api.solvershub.site` | `^/healthz$` | `http://localhost:3003` | salud |
| `app.solvershub.site` | `^/webhooks/` | `http://localhost:3003` | continuidad: Meta y Wompi tienen registradas las URL de `PUBLIC_BASE_URL` (`https://app.solvershub.site/webhooks/…`) |
| `app.solvershub.site` | `^/healthz$` | `http://localhost:3003` | salud |
| `app.solvershub.site` | *(vacío)* | `http://localhost:3004` | panel (incluye su BFF `/api/*`) |
| `solvershub.site` | *(vacío)* | `http://localhost:3005` | landing |
| `www.solvershub.site` | *(vacío)* | `http://localhost:3005` | landing (canónico: `NEXT_PUBLIC_SITE_URL`) |
| cualquier otro | — | `http_status:404` | regla final del túnel |

**Rutas públicas de la API: solo `/webhooks/*` y `/healthz`.** `/api/v1` no tiene ruta en el
túnel: el panel lo consume por la red interna (`AGENT_CORE_API_URL`), y la landing también llama a
la API por esa red (intake de leads y chat web). Rutas de la API que existen pero no se publican:
`/api/v1/*` (`core/api/__init__.py:42`), `/api/pack/*` (`packages/pack-commerce/pack_commerce/api.py:54`,
`packages/pack-cobranza/pack_cobranza/api.py:51`) y el panel clásico `/admin` (ADR-0076). Lo que
sí entra por `/webhooks/` (`core/gateway/webhooks.py:45`): `/webhooks/whatsapp`,
`/webhooks/whatsapp/{slug}`, `/webhooks/billing/wompi` y `/webhooks/payments/wompi/{slug}`
(`packages/pack-payments/pack_payments/module.py:39`).

Access (Zero Trust → Access → Applications). Cloudflare aplica la aplicación de path más
específico:

| Aplicación de Access | Política | Estado |
| --- | --- | --- |
| `app.solvershub.site` | **Allow**: correos del equipo | ya existe (la usa la producción heredada) |
| `app.solvershub.site/webhooks` y `app.solvershub.site/healthz` | **Bypass**: Everyone | ya existen |
| `api.solvershub.site` | **Allow**: correos del equipo (defensa en profundidad: el túnel solo enruta `/webhooks/` y `/healthz`) | nueva |
| `api.solvershub.site/webhooks` y `api.solvershub.site/healthz` | **Bypass**: Everyone | nuevas |
| `solvershub.site`, `www.solvershub.site` | sin Access: la landing es pública | — |

> Con la política **Allow** del equipo en `app.solvershub.site`, un negocio cliente no puede
> entrar al panel. Abrirlo a los clientes es una decisión pendiente (runbook, "Decisiones pendientes antes del corte").

Account `0c0150bffadc10e3f8b01426cfc76ada`, zona `solvershub.site`
`f34c3ab32cc37679cd556455a17e9224`.

### Registros DNS

La zona `solvershub.site` ya está en Cloudflare (HU-GTM-01). Los registros del túnel los crea
Cloudflare al agregar cada ruta; ninguno apunta a una IP.

| Nombre | Tipo | Contenido | Proxy | Cómo se crea |
| --- | --- | --- | --- | --- |
| `solvershub.site` (apex) | CNAME aplanado | `<uuid-túnel-prod>.cfargotunnel.com` | Proxied | al crear la ruta |
| `www` | CNAME | `<uuid-túnel-prod>.cfargotunnel.com` | Proxied | al crear la ruta |
| `api` | CNAME | `<uuid-túnel-prod>.cfargotunnel.com` | Proxied | al crear la ruta |
| `app` | CNAME | hoy `442da5f7-a3fa-4226-9450-2951be30b9d3.cfargotunnel.com` (túnel heredado) | Proxied | **en el corte**: se quita del túnel heredado y se agrega al nuevo (runbook, paso 8.10) |
| `test` | CNAME | `88c17ba2-7a02-4300-8a7a-a154d32804df.cfargotunnel.com` (túnel de test) | Proxied | sin cambios |
| MX, SPF, DKIM, DMARC y demás registros del correo | — | — | — | **no se tocan** |

Si `solvershub.site` o `www` ya apuntan a otro hosting, ese registro se borra en el momento de
crear la ruta (el panel de Cloudflare no deja dos registros con el mismo nombre).

### Variables por recurso

Solo nombres y de dónde sale cada valor; **los valores nunca se escriben en el repo**. Se cargan
en Coolify (Configuration → Environment Variables) y se guardan también en el gestor de
contraseñas del dueño (D-SPEC-01-3). Un cambio de variable exige **Redeploy**, no Restart.

Origen: **fijo** (el valor está acá), **generar** (secreto nuevo para producción), **heredada**
(copiar de la producción heredada: los datos migrados o un registro externo dependen de ese
valor), **Coolify** (lo genera el recurso), **runbook** (lo crea un paso del runbook),
**proveedor** (consola de un servicio externo).

#### Motor: `agent-core-api`, `agent-core-worker` y `agent-core-beat`

Columnas A/W/B: la variable va en la API, el worker o el beat. Fuente de verdad de los nombres:
`core/config.py` (y `.env.example`).

| Variable | A | W | B | Origen y valor |
| --- | --- | --- | --- | --- |
| `AGENT_CORE_ROLE` | ✓ | ✓ | ✓ | fijo: `api`, `worker`, `beat`. Explícito en los tres: si el worker hereda `api`, migra al arrancar (`scripts/entrypoint-worker.sh:14`, `scripts/entrypoint.sh:41-47`) |
| `DATABASE_URL` | ✓ | ✓ | ✓ | runbook: `postgresql+asyncpg://app_runtime:<clave>@<ip-privada-data>:5432/agent_core`. Clave **generada** al crear `app_runtime`, nunca la de desarrollo |
| `MIGRATIONS_DATABASE_URL` | ✓ | — | — | runbook: rol dueño `agent_core_migrations`. Solo la API migra; worker y beat no reciben la credencial del dueño |
| `REDIS_URL` | ✓ | ✓ | ✓ | Coolify: `redis://default:<clave>@<ip-privada-data>:6379/0` |
| `TENANTS_ROOT` | ✓ | ✓ | ✓ | fijo: `/app/tenants` (el volumen) |
| `APP_ENV` | ✓ | ✓ | ✓ | fijo: `production`, marcada *Not available during build* |
| `APP_NAME` | ✓ | ✓ | ✓ | opcional |
| `APP_SECRET_KEY` | ✓ | ✓ | ✓ | generar: `openssl rand -hex 32` (al cambiarla se cierran las sesiones del panel) |
| `PUBLIC_BASE_URL` | ✓ | ✓ | ✓ | fijo: `https://app.solvershub.site`. Es la base de las URL de webhook que el panel muestra y que están registradas en Meta y Wompi (`core/tenants.py:80-89`); cambiarla a `api.` es parte de HU-ARCH-02 |
| `PANEL_BASE_URL` | ✓ | ✓ | ✓ | fijo: `https://app.solvershub.site` (retorno de OAuth de Google, ADR-0093) |
| `LANDING_BASE_URL` | ✓ | ✓ | ✓ | fijo: `https://www.solvershub.site` (fragmento del chat web) |
| `ROOT_PATH` | ✓ | ✓ | ✓ | fijo: vacío |
| `TRUSTED_PROXY_CIDRS` | ✓ | ✓ | ✓ | sin definir: el valor por defecto (redes privadas y loopback, `core/config.py:76-79`) cubre cloudflared y la red de Coolify |
| `TOKEN_MASTER_KEYS` | ✓ | ✓ | ✓ | heredada **más** una clave nueva generada para producción: sin las claves viejas no se descifran los secretos migrados (`core/shared/crypto.py:1-21`) |
| `TOKEN_ACTIVE_KEY_ID` | ✓ | ✓ | ✓ | el id de la clave nueva |
| `MASTER_ENCRYPTION_KEY` | ✓ | ✓ | ✓ | heredada (solo lee datos viejos en formato Fernet) |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_MODEL_REASONING`, `LLM_MODEL_FALLBACK`, `LLM_BASE_URL`, `LLM_MAX_TOKENS`, `DIFFICULTY_ROUTING`, `LLM_THINKING_DEFAULT`, `LLM_THINKING_REASONING` | ✓ | ✓ | ✓ | heredada (no son secretos) |
| `LLM_API_KEY` | ✓ | ✓ | ✓ | proveedor: llave **propia de producción**, con tope de gasto; nunca la de test |
| `OPENAI_API_KEY` | ✓ | ✓ | ✓ | proveedor: llave de producción (respaldo `gpt-4o-mini` y embeddings `openai`) |
| `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `MEMORY_RETENTION_DAYS` | ✓ | ✓ | ✓ | heredada: la dimensión tiene que coincidir con los vectores migrados |
| `WHATSAPP_VERIFY_TOKEN` | ✓ | ✓ | ✓ | heredada: es el que Meta tiene registrado |
| `WHATSAPP_APP_SECRET` | ✓ | ✓ | ✓ | heredada: firma del webhook (fail-closed, HU-ARCH-05) |
| `WEBHOOK_ALLOW_UNSIGNED` | ✓ | ✓ | ✓ | fijo: `false` (`core/config.py:128-131`) |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | ✓ | ✓ | ✓ | heredada, solo si la heredada los tiene (las conexiones viven cifradas en la BD) |
| `META_APP_ID`, `META_APP_SECRET`, `META_CONFIG_ID`, `META_GRAPH_VERSION` | ✓ | ✓ | ✓ | proveedor: la app de Meta **de producción**, nunca la de test (HU-ARCH-02) |
| `DEV_APP_SECRET_HASH` | ✓ | — | — | generar: sha256 del App Secret de desarrollo (check `prod_secret_differs_from_dev`) |
| `META_SERVICE_MESSAGE_ALLOWANCE_HYPOTHESIS_ENABLED` | ✓ | ✓ | ✓ | heredada (por defecto `false`) |
| `WAWEB_ENABLED` | ✓ | ✓ | ✓ | fijo: `false` (P-08; no hay recurso conector) |
| `DEMO_MODE` | ✓ | ✓ | ✓ | fijo: `false` |
| `DEBOUNCE_MODE`, `MAX_AGENT_ITERATIONS`, `MESSAGE_SPLIT_STYLE`, `MAX_CONCURRENT_TURNS`, `MAX_TURNS_PER_TENANT`, `INBOUND_RECLAIM_IDLE_MS`, `CLAIM_STALE_SECONDS`, `TURN_TIMEOUT_S`, `TURN_QUEUE_TIMEOUT_S`, `TURN_SLOW_WARN_S` | ✓ | ✓ | ✓ | heredada, o los valores por defecto de `core/config.py` |
| `BILLING_PROVIDER`, `BILLING_CURRENCY`, `BILLING_TRIAL_DAYS` | ✓ | ✓ | ✓ | heredada |
| `WOMPI_BILLING_PUBLIC_KEY`, `WOMPI_BILLING_PRIVATE_KEY`, `WOMPI_BILLING_EVENTS_SECRET` | ✓ | ✓ | ✓ | proveedor: llaves de producción del comercio de la plataforma |
| `WOMPI_BILLING_SANDBOX` | ✓ | ✓ | ✓ | `false` solo con llaves de producción |
| `SYSTEM_SMTP_HOST`, `SYSTEM_SMTP_PORT`, `SYSTEM_SMTP_USERNAME`, `SYSTEM_SMTP_PASSWORD`, `SYSTEM_SMTP_FROM` | ✓ | ✓ | ✓ | proveedor de correo (buzones de HU-GTM-01) |
| `PROVISIONING_SELLER_TENANTS` | ✓ | ✓ | — | fijo: **vacía** hasta HU-SH-04 (ADR-0121): el plan pago que crea el alta automática todavía no se renueva y `past_due`/`expired` bloquean en duro (`core/billing/enforcement.py:30`). UUID separados por coma; la necesita el **worker**, donde corre el despachador del outbox. En test, el UUID de `solvershub` para verificar HU-SH-03. El correo con el acceso necesita `SYSTEM_SMTP_*`. Altas en revisión: `docs/runbooks/altas-en-revision.md` |
| `EXPO_PUSH_ENABLED`, `EXPO_PUSH_URL`, `EXPO_PUSH_ACCESS_TOKEN` | ✓ | ✓ | ✓ | `EXPO_PUSH_ENABLED=false` hasta que la app oficial pase a producción |
| `LOG_LEVEL` | ✓ | ✓ | ✓ | fijo: `INFO` |
| `SENTRY_ENABLED`, `SENTRY_DSN` | ✓ | ✓ | ✓ | proveedor: proyecto de producción en Sentry |
| `SENTRY_ENVIRONMENT` | ✓ | ✓ | ✓ | fijo: `production`. La lee `sentry-sdk`, no `core/config.py`; su valor por defecto ya es `production`, por eso **test** tiene que fijar `test` |
| `OTEL_ENABLED` | ✓ | ✓ | ✓ | fijo: `true` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | ✓ | ✓ | ✓ | proveedor: URL **completa** de trazas, terminada en `/v1/traces`: el core la pasa tal cual al exportador (`core/observability/tracing.py:36-38`) |
| `OTEL_EXPORTER_OTLP_HEADERS` | ✓ | ✓ | ✓ | proveedor: credencial del servicio OTLP (la lee el SDK de OpenTelemetry del entorno, no `core/config.py`) |
| `OTEL_SERVICE_NAME` | ✓ | ✓ | ✓ | fijo: `agent-core-api`, `agent-core-worker`, `agent-core-beat` |
| `SLACK_ALERTS_WEBHOOK_URL` | ✓ | ✓ | ✓ | proveedor, opcional |

Coolify inyecta `SOURCE_COMMIT` y `COOLIFY_BRANCH`: los usa el checklist de producción para decir
qué commit está desplegado.

#### Panel: `panel-web`

| Variable | Origen y valor |
| --- | --- |
| `AGENT_CORE_API_URL` | fijo: `http://agent-core-api:8000` (alias de red de la API). Se lee en runtime, del lado servidor (`apps/panel-web/lib/api/server.ts:10` en `solvershub-panel`) |
| `ORIGINS_PERMITIDAS` | vacía, salvo orígenes extra para el BFF (`apps/panel-web/middleware.ts:18`) |

#### Landing: `landing`

Las `NEXT_PUBLIC_*` se hornean en el build (Coolify las pasa como build args, `Dockerfile` de
`solvershub-landing`): si cambian, hay que reconstruir la imagen.

| Variable | Cuándo | Origen y valor |
| --- | --- | --- |
| `NEXT_PUBLIC_SITE_URL` | build | fijo: `https://www.solvershub.site` |
| `NEXT_PUBLIC_PANEL_URL` | build | fijo: `https://app.solvershub.site` (botón "Iniciar sesión", `lib/site.ts:13-14`) |
| `NEXT_PUBLIC_WHATSAPP_NUMBER`, `NEXT_PUBLIC_SCHEDULE_URL` | build | datos del negocio |
| `NEXT_PUBLIC_POSTHOG_KEY`, `NEXT_PUBLIC_POSTHOG_HOST` | build | proveedor (opcional) |
| `LEAD_WEBHOOK_URL` | runtime | fijo: `http://agent-core-api:8000/api/pack/pack_commerce/leads/<slug del negocio solvershub>` |
| `LEAD_WEBHOOK_BASE` | runtime | fijo: `http://agent-core-api:8000/api/pack/pack_commerce` (formulario alojado `/f/<slug>`) |
| `WEBCHAT_API_BASE` | runtime | fijo: `http://agent-core-api:8000/api/v1/public/web-chat`. Explícita: si falta, la landing la deriva de `LEAD_WEBHOOK_BASE` (`lib/webchat.ts`) |
| `LEAD_WEBHOOK_SECRET` | runtime | heredada: el secreto del intake configurado en el pack de comercio del negocio `solvershub` |
| `RESEND_API_KEY`, `LEAD_NOTIFY_EMAIL`, `LEAD_FROM_EMAIL` | runtime | proveedor, opcional (aviso por correo si no hay webhook) |

#### Datos y borde

| Recurso | Variable o dato | Origen |
| --- | --- | --- |
| `agent-core-postgres` | `POSTGRES_USER=postgres`, `POSTGRES_DB=agent_core` | fijo |
| `agent-core-postgres` | `POSTGRES_PASSWORD` | Coolify. Es el superusuario: solo para administrar, restaurar y contar filas; **nunca** va en una app |
| `agent-core-postgres` | claves de `agent_core_migrations` y `app_runtime` | runbook (paso 4.3): se generan al crear los roles |
| `agent-core-redis` | contraseña | Coolify |
| `cloudflared` | token del túnel `solvershub-prod` | proveedor (Zero Trust). Vive en el servicio systemd de `prod-app`, no en Coolify ni en el repo |

### Secretos: dónde viven y cómo se rotan

D-SPEC-01-3: los secretos son variables de Coolify, con copia en el gestor de contraseñas del
dueño. Si se pierde el Coolify de `prod-app`, se reconstruye con este inventario y esa copia.

| Secreto | Cómo se rota | Qué se redespliega | Efecto |
| --- | --- | --- | --- |
| `APP_SECRET_KEY` | generar y reemplazar | api, worker, beat | se cierran las sesiones del panel |
| Clave de `TOKEN_MASTER_KEYS` | agregar una clave con id nuevo y apuntar `TOKEN_ACTIVE_KEY_ID` a ella; la vieja **se queda** mientras haya filas cifradas con ella (no hay re-cifrado masivo: cada fila se re-cifra al guardarse) | api, worker, beat | ninguno |
| Clave de `app_runtime` | `ALTER ROLE` con `\password` en `psql` y luego `DATABASE_URL` | api, worker, beat | corte breve |
| Clave de `agent_core_migrations` | `ALTER ROLE` y `MIGRATIONS_DATABASE_URL` | api | ninguno |
| Superusuario `postgres` y clave de Redis | en el recurso de Coolify; para Redis, también `REDIS_URL` | api, worker, beat | rotar Redis en una ventana: hay colas en vuelo |
| `LLM_API_KEY`, `OPENAI_API_KEY` | consola del proveedor | api, worker, beat | ninguno |
| `WHATSAPP_APP_SECRET`, `META_APP_SECRET` | app de Meta | api, worker, beat | coordinar con Meta: cambia la firma del webhook |
| Llaves de Wompi | panel de Wompi | api, worker, beat | ninguno |
| `SENTRY_DSN`, `OTEL_EXPORTER_OTLP_HEADERS` | consola del proveedor | api, worker, beat | ninguno |
| Token del túnel | Zero Trust → el túnel → rotar token; en `prod-app`, `cloudflared service uninstall` y `cloudflared service install <token nuevo>` | — | segundos sin tráfico |

### Separación de test

| Aspecto | Producción | Test |
| --- | --- | --- |
| Servidor | `prod-app` y `prod-data` (VPS) | máquina de desarrollo |
| Coolify | instancia propia en `prod-app` | Coolify local (`127.0.0.1:8000`) |
| Base y Redis | `agent-core-postgres` y `agent-core-redis` en `prod-data` | contenedores locales (sección Test) |
| Roles de Postgres | dueño `agent_core_migrations` (BYPASSRLS, sin superusuario) y `app_runtime` con clave propia | migraciones como `postgres`; `app_runtime` con la clave de test |
| Llaves de LLM, Meta y Wompi | de producción | de prueba o sandbox |
| Claves de cifrado | las de la heredada más una activa nueva | propias: no pueden coincidir con las de producción |
| Túnel | `solvershub-prod` | `solvershub-test` |
| Sentry | `SENTRY_ENVIRONMENT=production` | `SENTRY_ENVIRONMENT=test` |

Para comprobar que no comparten secretos sin mostrarlos, se comparan hashes. En cada entorno, en
el contenedor de la API:

```bash
docker exec <contenedor-api> sh -c 'for v in APP_SECRET_KEY TOKEN_MASTER_KEYS MASTER_ENCRYPTION_KEY \
  LLM_API_KEY OPENAI_API_KEY WHATSAPP_APP_SECRET META_APP_SECRET WOMPI_BILLING_PRIVATE_KEY; do
  printf "%s " "$v"; printenv "$v" | sha256sum | cut -c1-16; done'
```

Ningún hash puede repetirse entre producción y test, salvo el de una variable sin definir
(`e3b0c44298fc1c14`) o vacía (`01ba4719c80b6fe9`) en los dos. Las claves de Postgres y Redis de
producción son nuevas (runbook y Coolify), y `scripts/verificar_aislamiento.py` falla si el
runtime usa la clave de desarrollo. Si `TOKEN_MASTER_KEYS` coincide, test tiene las claves con
que se cifraron los datos de producción: se le quitan y test se re-siembra con claves propias.

### Observabilidad

- **Errores: Sentry** (D-SPEC-01-2). La API lo activa al arrancar y el worker también
  (`core/main.py:44-46`, `core/tasks/celery_app.py`); sin `SENTRY_ENABLED` y `SENTRY_DSN` no
  hace nada. Envía sin datos personales (`send_default_pii=False`) y tapa tokens y claves
  (`core/observability/sentry_setup.py`). El panel todavía no reporta a Sentry (HU-ARCH-07).
- **Trazas: OTLP** a un servicio gestionado con plan gratuito (D-SPEC-01-2): spans por turno,
  llamada al LLM y tool (`core/observability/tracing.py`). Solo trazas: el core no exporta
  métricas ni logs por OTLP. Cuidado con la URL (ver la tabla de variables).
- **Logs**: los de los contenedores, en Coolify. El instalador de Coolify deja Docker con rotación
  de logs (`json-file`, 10 MB × 3) en `prod-app`; en `prod-data` hay que comprobarlo (runbook).
- **Worker y beat vivos**: el beat programa el latido cada minuto y el worker lo escribe en Redis
  (`worker:heartbeat`, TTL 180 s). Lo leen el checklist de producción del panel y
  `python scripts/verify_production.py` (check `worker_heartbeat_and_dlq`).
- **Aislamiento**: `python scripts/verificar_aislamiento.py` en el contenedor de la API (reutiliza
  el check `rls_forced` del checklist).
- **Alertas, `/healthz` profundo, latencia del webhook y costo por hora**: HU-ARCH-07. Hoy
  `/healthz` es estático (`core/main.py:140-142`): dice que el proceso responde, no que la base o
  Redis estén bien.

## Producción heredada (hasta el corte)

Lo que hoy atiende `app.solvershub.site`. Corre en el Coolify local de la máquina de desarrollo,
en el entorno `production` del proyecto **SolversHub** (`xqrxaj7by6yclupym5u9rcvj`). El runbook la
congela, copia sus datos al VPS y la apaga (fases 8 y 11). **Se borra esta sección cuando termine
el periodo de vuelta atrás.** El hook `.claude/hooks/guards.py` (`PROD_RESOURCES`) y las skills
`release` y `desplegar-test-agent-core` apuntan a estos UUID hasta entonces.

Entorno `production` (`mihlppg36bf846gpu7zfu2h3`):

| Recurso | Nombre | UUID | Detalles |
| --- | --- | --- | --- |
| Postgres | agent-core-postgres | `huvhzaxim2notqkcxqa5930a` | imagen propia `127.0.0.1:5000/solvershub-postgres:pg17-postgis`, db `agent_core`, publicado en `127.0.0.1:5439` |
| Redis | agent-core-redis | `gk9bagcojtdrtofcx8tdxkiq` | `redis:7.2`, publicado en `127.0.0.1:6381` |
| App | agent-core | `ggzjdyc7zsemt6kzafx5rg7m` | `devrochy/agent-core` · `main` · `127.0.0.1:3003:8000` · health `/healthz` |
| Beat | agent-core-beat | `hwh7s5gscnn1k3yqf88xceja` | Celery beat |
| Worker | agent-core-worker | `wv6kycjeae0lmfj11dprkasq` | Celery worker |

En Coolify la app, el worker y el beat de la heredada figuran como `agent-core-test`,
`agent-core-test-worker` y `agent-core-test-beat` (por herencia): guiarse siempre por el UUID y la
etiqueta `coolify.environmentName`.

- Túnel **`solvershub`** (`442da5f7-a3fa-4226-9450-2951be30b9d3`): token, configuración remota
  (dashboard): `app.solvershub.site → http://localhost:3003` (todo el hostname a la API, incluido
  el panel clásico `/admin`). El conector corre con `~/.cloudflared/run-solvershub.sh` (crontab
  `@reboot`).
- Access: `app.solvershub.site` con **Allow** (equipo) y **Bypass** en `/webhooks/*` y `/healthz`.
- Roles: el runtime conecta como `app_runtime` **con la clave de desarrollo** que pone la
  migración 0003 (`alembic/versions/0003_multitenant_core.py:236`); las migraciones, como
  `agent_core_migrations`. El VPS no repite esto: ver [Variables por recurso](#variables-por-recurso).
- No tiene panel nuevo ni landing.

## Test (máquina de desarrollo)

Entorno `test` (`vs22dwtxgnmlfelvkeft3flt`) del proyecto **SolversHub** en el Coolify local:

| Recurso | Nombre | UUID | Detalles |
| --- | --- | --- | --- |
| Postgres | agent-core-test-postgres | `48h2r2laml64eyrggfg6eqld` | **imagen propia** `127.0.0.1:5000/solvershub-postgres:pg17-postgis` (pgvector + PostGIS), db `agent_core` |
| Redis | agent-core-test-redis | `rqkxxnen7ysp7xmqhocrbidw` | `redis:7.2` |
| App | agent-core-test | `tr0vpdei5av6szcgwpe1vwcg` | `devrochy/agent-core` · `develop` · `3013:8000` · health `/healthz` |
| Panel | panel-web-test | `eyfamlms7ipmba4h6cct2i4a` | `devrochy/solvershub-panel` · `main` · base `/apps/panel-web` · `3014:3000` · health `/login` |
| Beat | clone-of-agent-core-test-beat | `set9kgdxianzvsmprklzyym7` | Celery beat |
| Worker | clone-of-agent-core-test-work | `uqfcdd4a3cf5d4iqa0uosrp0` | Celery worker |
| Conector waweb | agent-core-test-connector | `yqd5gq5j1r1wrgqtjedom16m` | ver [Conector de WhatsApp Web](#conector-de-whatsapp-web-qr-solo-test) |

Variables que cambian respecto de producción: `PUBLIC_BASE_URL=https://test.solvershub.site`;
`DATABASE_URL` apunta a `48h2r2laml64eyrggfg6eqld:5432` como `app_runtime`;
`MIGRATIONS_DATABASE_URL` usa el superusuario `postgres` de ese contenedor; `REDIS_URL` apunta a
`rqkxxnen7ysp7xmqhocrbidw:6379`; `APP_ENV=production`; si test activa Sentry,
`SENTRY_ENVIRONMENT=test`. El resto de los secretos (`APP_SECRET_KEY`, `TOKEN_*`,
`MASTER_ENCRYPTION_KEY`, `LLM_*`, Meta, `WOMPI_*`) tienen que ser **propios de test**
([Separación de test](#separación-de-test)).

El panel de test llama a la API por **red interna**:
`AGENT_CORE_API_URL=http://172.18.0.1:3013` (gateway del host; el nombre del contenedor cambia en
cada deploy). En el VPS se usa el alias de red de la API, que no cambia.

### Túnel y Access de test

Túnel **`solvershub-test`** (`88c17ba2-7a02-4300-8a7a-a154d32804df`): **local**
(`~/.cloudflared/solvershub-test.yml`), con ruteo por path:

```
test.solvershub.site  ^/api/pack/pack_cobranza(/|$) → localhost:3013   (API del pack / APK)
test.solvershub.site  ^/api/v1(/|$)                 → localhost:3013   (API JSON de la app oficial, SPEC-25)
test.solvershub.site  ^/webhooks(/|$)               → localhost:3013
test.solvershub.site  ^/healthz$                    → localhost:3013
test.solvershub.site  (resto, incl. /api/* del BFF) → localhost:3014   (panel)
```

> La regla `^/api/v1` se agregó el 2026-10-06 para la app oficial (login admin, search, sync,
> push-token, audit). `/api/v1/*` sigue detrás de Cloudflare Access en test, así que la app móvil
> (sin sesión de Access) no puede consumirlo por el dominio; hoy se prueba por LAN
> (`http://192.168.40.15:3013`). En **producción** `/api/v1` no es público (HU-ARCH-01): exponerlo
> para la app oficial es una decisión de SPEC-25 / HU-ARCH-08, no de esta topología.

El conector corre con `~/.cloudflared/run-solvershub-test.sh` (crontab `@reboot`). El panel y la
API **comparten el prefijo `/api`**: por eso el ruteo es fino (`/api/pack/pack_cobranza/*` a la
API; el resto de `/api/*` al panel).

En test hay una app de Access por hostname y una por cada ruta que tiene que quedar pública (la
APK no puede hacer SSO):

| Dominio (app de Access) | Policy |
| --- | --- |
| `test.solvershub.site` | **Allow** (emails del equipo) |
| `test.solvershub.site/api/pack/pack_cobranza` | **Bypass** (API de campo / APK) |
| `test.solvershub.site/webhooks` | **Bypass** (Meta) |
| `test.solvershub.site/healthz` | **Bypass** (health check) |

Para sumar gente al `Allow`, editar la policy en Zero Trust → Access → Applications.

### Conector de WhatsApp Web (QR, solo test)

El canal `waweb` (ADR-0074) necesita un servicio **aparte** del api: sin él, el panel muestra la
tarjeta del QR pero nadie la genera/publica en Redis. En producción no existe (P-08).

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

## Persistencia de artefactos de tenant (`TENANTS_ROOT`)

Los artefactos por tenant (`flow.json`, `playbooks/`, `catalog.json`, `docs/` y las evidencias de
cobranza en `evidencias/`) viven en **`TENANTS_ROOT`** (`/app/tenants`) en el filesystem
(ADR-0085). **Sin volumen se pierden en cada redeploy.**

- **`docker-compose.yml`**: volumen nombrado **`tenants-data`** montado en `/app/tenants` en
  `core-api`, `celery-worker` y `celery-beat`. El entrypoint hace `mkdir -p $TENANTS_ROOT`
  (`scripts/entrypoint.sh:20`).
- **Coolify**: el **mismo volumen nombrado** (`agent-core-tenants` en producción) montado en
  `/app/tenants` en la API, el worker y el beat; con nombres distintos se desincronizan. Los tres
  están en `prod-app`, así que comparten el volumen de Docker.
- **Dueño del volumen**: la imagen corre como `app` (`Dockerfile:12`, `:37-38`) pero no trae
  `/app/tenants`, así que un volumen nuevo nace de `root` y `app` no puede escribir en él
  (comprobado con la misma base de imagen). Se arregla una vez, después del primer deploy:
  `docker exec -u root <contenedor-api> chown -R app:app /app/tenants`. Si el `Dockerfile` crea
  el directorio antes del `chown -R app:app /app`, Docker inicializa el volumen con ese dueño y el
  paso sobra.
- **Backup**: el volumen entra en los backups de HU-ARCH-03. Mientras tanto, el runbook lo copia
  en el corte con `tar`.
- Pendiente: mover `flow`/playbooks a BD (versionado), ver ADR-0085.

## Despliegue

**La imagen hornea los packs** (`packages/pack-*` y `plugins/`) y el Dockerfile declara
`ENTRYPOINT ["/app/scripts/entrypoint.sh"]`: sin él las migraciones no corren. Migraciones y
plugins corren en el **arranque del contenedor**; los pre/post-deployment de Coolify están en
`echo skip`. Solo la API migra (`AGENT_CORE_ROLE=api`).

**Test** (skill `desplegar-test-agent-core`), contra el Coolify local:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/applications/<uuid>/start"   # deploy de una app
```

**Producción** (skill `release`), contra el Coolify de `prod-app`. Su API no se publica: se llega
con un túnel SSH a un puerto local distinto del Coolify de test, y el token es el de esa
instancia:

```bash
ssh -N -L 127.0.0.1:8100:127.0.0.1:8000 <usuario>@<ip-publica-app> &
curl -X POST -H "Authorization: Bearer $TOKEN_PROD" \
  "http://127.0.0.1:8100/api/v1/applications/<uuid-api>/start"
```

Orden: la API primero (migra); cuando está `healthy`, el worker y el beat con el mismo commit; el
panel y la landing cuando cambian.

## Sembrar datos de demo (solo test)

El contenedor trae `scripts/seed_demo_login.py` (ADR-0081): crea dos negocios con
`pack_cobranza`, un gestor compartido, un invitado con invitación pendiente y un
master. Todos con contraseña `demo12345`. **Nunca en producción.**

```bash
APP=$(docker ps --format '{{.Names}}' | grep tr0vp | head -1)
docker exec "$APP" python scripts/seed_demo_login.py
```

## Verificación post-despliegue

Producción (VPS):

```bash
# en prod-app
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E '<uuid-api>|<uuid-worker>|<uuid-beat>|<uuid-panel>|<uuid-landing>'
docker exec <contenedor-api> python scripts/verificar_aislamiento.py   # rls_forced, rol del runtime
docker exec <contenedor-api> python scripts/verify_production.py       # checklist completo (latido del worker incluido)
# desde fuera
curl -s https://app.solvershub.site/healthz                            # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" https://api.solvershub.site/api/v1/auth/login   # 302 a Access o 404: nunca la API
# en prod-data: migraciones al head del repo (core y cada pack)
docker exec <contenedor-postgres> psql -U postgres -d agent_core -tAc "SELECT version_num FROM alembic_version;"
```

Test:

```bash
curl -s https://test.solvershub.site/healthz                       # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" https://test.solvershub.site/login   # 200
# login unificado (ADR-0081): el gestor demo debe devolver seleccionRequerida
curl -s -X POST https://test.solvershub.site/api/pack/pack_cobranza/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"usuario":"gestor@demo.test","password":"demo12345"}'
# migraciones al head
docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -d agent_core -tAc "SELECT version_num FROM alembic_version;"
# el rol de runtime existe
docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -d agent_core -tAc "SELECT rolname FROM pg_roles WHERE rolname='app_runtime';"
```

## Gotchas conocidos

- **Docker se salta `ufw`.** Los puertos que publica Docker no los frena `ufw` (lo advierte la
  documentación de Coolify, "Firewall"). En el VPS la barrera es el **firewall del proveedor**;
  además, los puertos de las apps se publican solo en `127.0.0.1`.
- **PostGIS no viene en `pgvector/pgvector`.** El pack-cobranza exige la extensión
  (ADR-0058); la imagen oficial no la trae y las migraciones fallan
  (`Could not open extension control file … postgis`). Se usa una **imagen propia**:
  `docker/postgres/Dockerfile` (`FROM pgvector/pgvector:pg17` + `postgresql-17-postgis-3`).
  Coolify hace `docker compose pull` **en cada arranque**, así que la imagen debe ser
  **pullable** (una imagen solo local no sirve). Se publica en un **registry local** del mismo
  servidor:

  ```bash
  docker run -d --name solvershub-registry --restart always -p 127.0.0.1:5000:5000 registry:2
  docker build -t solvershub-postgres:pg17-postgis docker/postgres
  docker tag solvershub-postgres:pg17-postgis 127.0.0.1:5000/solvershub-postgres:pg17-postgis
  docker push 127.0.0.1:5000/solvershub-postgres:pg17-postgis
  # en Coolify, la DB usa: 127.0.0.1:5000/solvershub-postgres:pg17-postgis
  ```

  Docker trata `127.0.0.1` como registry inseguro permitido (no hace falta tocar
  `daemon.json`). **Dependencia:** el registry debe estar arriba para reiniciar la
  DB; corre con `--restart always`. En el VPS se usa la imagen de GHCR que publica
  `ci-postgres-image.yml` (la misma del CI, ADR-0120); el registry local queda como alternativa.
- **`vector` y `postgis` exigen superusuario para `CREATE EXTENSION`; `btree_gist` no.** Si el
  dueño de las migraciones no es superusuario, el superusuario crea las extensiones antes de la
  primera migración (las migraciones usan `IF NOT EXISTS`). Comprobado el 2026-10-09: todas las
  migraciones (core y packs) corren con un dueño `NOSUPERUSER BYPASSRLS` dueño de la base, con
  las extensiones y `app_runtime` creados antes.
- **El rol de migraciones necesita `BYPASSRLS` (o ser superusuario).** La `0034` falla con un
  dueño sin bypass y la `0052` aborta a propósito (`docs/BACKLOG.md`).
- **`app_runtime` nace con la clave de desarrollo** si no existe cuando corre la migración 0003
  (`alembic/versions/0003_multitenant_core.py:226-249`). En producción se crea antes, con clave
  propia; `scripts/verificar_aislamiento.py` falla si la URL del runtime trae la de desarrollo.
- **Volumen nuevo de `TENANTS_ROOT` sin permiso de escritura**: ver
  [Persistencia](#persistencia-de-artefactos-de-tenant-tenants_root).
- **`AGENT_CORE_ROLE` explícito en worker y beat**: si heredan `api`, migran al arrancar.
- **OTLP**: `OTEL_EXPORTER_OTLP_ENDPOINT` va con la ruta completa (`…/v1/traces`); el SDK solo
  agrega la ruta cuando lee la variable él mismo, y el core le pasa el valor explícito.
- **Sentry**: sin `SENTRY_ENVIRONMENT`, `sentry-sdk` reporta como `production`; test debe fijar
  `test`.
- **Landing sin `curl` ni `wget`**: el health check HTTP de Coolify falla; queda desactivado y
  rige el `HEALTHCHECK` de la imagen.
- **Dos Coolify en juego** (test local y producción en `prod-app`): la API del de producción se
  usa por túnel SSH en otro puerto local (`8100`), nunca en `127.0.0.1:8000`, que es el de test.
- **Puertos de Coolify y loopback.** La API de Coolify **rechaza** el formato
  `127.0.0.1:puerto:puerto` en `ports_mappings` (solo acepta `hostPort:containerPort`,
  que publica en `0.0.0.0`). El loopback se configura en la **UI**. El túnel entra por
  `localhost`, así que cerrar el puerto no rompe el dominio.
- **App privada:** crear con `POST /api/v1/applications/private-github-app` y
  `github_app_uuid` (la GitHub App instalada); `/applications/public` falla en
  `git ls-remote` con `could not read Username for 'https://github.com'`.
- **Panel:** contexto de build `apps/panel-web` (`base_directory`), Dockerfile
  `/Dockerfile`; healthcheck HTTP a `127.0.0.1:3000/login` (requiere `curl` en la
  imagen, ya incluido). Con `output: "standalone"`.
- **`cloudflared tunnel route dns` por nombre puede fallar** (`Tunnel not found`);
  usar el **UUID** del túnel.
- **Un hostname solo apunta a un túnel**: para mover `app.solvershub.site` hay que quitar su
  ruta (y su CNAME) del túnel heredado antes de agregarla al nuevo.
- **Un contenedor atascado en crash-loop bloquea el siguiente deploy**
  (`No such container` al limpiar). `docker rm -f <container>` y redesplegar.
- **El webhook de Meta exige 443**: lo sirve Cloudflare (TLS en el edge).
- **Los valores de env de Coolify van cifrados** en su BD (Laravel): no
  insertarlos por SQL, usar la UI o la API.
- **`POST /applications/{uuid}/restart` no recrea el contenedor**: para que tomen
  efecto cambios de env hay que **redeploy** (`/start`).

## Operar por API (token)

El API de Coolify se activa en `/settings/advanced` → API access. Los tokens se
guardan con hash; se envía `Authorization: Bearer <id>|<plaintext>`. Cada instancia (test y
producción) tiene sus propios tokens.
