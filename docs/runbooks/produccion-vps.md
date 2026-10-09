# Runbook: producción en el VPS (HU-ARCH-01)

Pone la producción (motor/API, worker, beat, panel y landing) en el VPS dedicado de D-19, copia
los datos de la producción que hoy corre en la máquina de desarrollo ("la heredada") y deja la
evidencia de los criterios de la HU (issue [#329](https://github.com/devrochy/agent-core/issues/329)).

- **Qué se monta y con qué variables**: [`docs/despliegue-coolify.md`](../despliegue-coolify.md),
  fuente de verdad de la topología. Este runbook dice **en qué orden** y **cómo se verifica**.
- **Quién**: los pasos marcados **Dueño** solo los hace el dueño (contratos, cuentas, secretos,
  Cloudflare). Los marcados **Operador** los hace una persona con acceso SSH, que puede ser el
  mismo dueño. **Ninguno se corre desde una sesión de asistente**: tocan producción (ver
  `.claude/hooks/guards.py` y la skill `release`).
- **Duración estimada**: preparación, un día (fases 1 a 6); ensayo, una hora; ventana de corte,
  una a dos horas (se mide en el ensayo).

Marcadores que se reemplazan al ejecutar:

| Marcador | Qué es |
| --- | --- |
| `<ip-publica-app>`, `<ip-publica-data>` | IP públicas de los nodos (solo para SSH) |
| `<ip-privada-app>`, `<ip-privada-data>` | IP de la red privada del proveedor |
| `<uuid-api>`, `<uuid-worker>`, `<uuid-beat>`, `<uuid-panel>`, `<uuid-landing>`, `<uuid-postgres>`, `<uuid-redis>` | UUID de cada recurso en el Coolify de producción |
| `<contenedor-api>` y similares | nombre del contenedor: `docker ps --format '{{.Names}}' \| grep <uuid>`. El de las apps cambia en cada deploy; el de las bases es el UUID |

Registro de tiempos y evidencias: se completa la [tabla del final](#registro-de-la-ejecución).

## 0. Antes de empezar: lo que tiene que estar listo en el repo

- [ ] Este cambio (scripts y documentación) llegó a `main` con una release: la imagen tiene que
      traer `scripts/verificar_aislamiento.py`, `scripts/comparar_conteos.py` y
      `scripts/vaciar_base_nueva.sql`.
- [ ] **RLS forzada en `meta_private.meta_tokens`.** La migración `0029` solo recorrió `public`
      (`docs/BACKLOG.md`). Mientras no exista la migración que lo arregle,
      `verificar_aislamiento.py` falla en `rls_otros_esquemas`; `rls_forced` (el criterio literal
      de la HU, solo `public`) pasa igual. Decidir antes del corte: arreglarlo (recomendado) o
      aceptar el riesgo por escrito.
- [ ] (Opcional) El `Dockerfile` crea `/app/tenants` antes del `chown -R app:app /app`. Si no, se
      hace el paso 5.2.
- [ ] Decisiones del dueño cerradas (ver [Decisiones pendientes](#decisiones-pendientes-antes-del-corte)).
- [ ] La heredada corre el último `main`: el VPS va a desplegar el mismo commit y las dos bases
      tienen que estar en las mismas migraciones.

## 1. Dueño: contratar y preparar cuentas

### 1.1 VPS de dos nodos (D-19) — dimensionamiento: SUPUESTO A VALIDAR

| Nodo | vCPU | RAM | Disco | Lo que corre |
| --- | --- | --- | --- | --- |
| `prod-app` | 4 | 8 GB | 80 GB NVMe | Coolify, API, worker, beat, panel, landing, cloudflared y los builds de las imágenes |
| `prod-data` | 2 | 4 GB | 80 GB NVMe | Postgres 17 (pgvector + PostGIS) y Redis |

De dónde salen los números (medidos en los contenedores de **test** en reposo, 2026-10-09):
API ~145 MiB, worker ~800 MiB (Celery arranca un proceso por CPU: en 4 vCPU usa menos), beat
~130 MiB, panel ~60 MiB, Coolify ~500 MiB, Postgres ~90 MiB, Redis ~6 MiB. `prod-app` queda en
~2 GB en reposo; el resto es margen para los builds de Next.js y de la imagen de Python (picos de
2 a 3 GB) y para picos de tráfico. Si se adoptan embeddings locales en producción (P-19),
`prod-app` pasa a 16 GB.

Validar antes de comprar, en la máquina de desarrollo (solo lectura):

```bash
docker stats --no-stream $(docker ps -q -f name=ggzjdyc7zsemt6kzafx5rg7m -f name=wv6kycjeae0lmfj11dprkasq \
  -f name=hwh7s5gscnn1k3yqf88xceja -f name=huvhzaxim2notqkcxqa5930a -f name=gk9bagcojtdrtofcx8tdxkiq)
docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -d agent_core -tAc \
  "select pg_size_pretty(pg_database_size('agent_core'))"
docker exec $(docker ps -q -f name=ggzjdyc7zsemt6kzafx5rg7m | head -1) du -sh /app/tenants
```

El disco de `prod-data` tiene que cubrir al menos tres veces el tamaño de la base (base, WAL y una
copia temporal en el corte).

Requisitos del contrato:

- Los dos nodos en el **mismo proveedor y región**, con **red privada** entre ellos.
- **Firewall del proveedor** (fuera del servidor). Docker se salta `ufw`.
- Ubuntu 24.04 LTS, amd64, acceso SSH con llave.
- Región: SUPUESTO A VALIDAR una región de EE. UU. este (latencia baja a Colombia y a las APIs de
  Meta y del LLM). Validar con el abogado la transferencia internacional de datos (Ley 1581) y
  agregar al proveedor como subencargado en el DPA (HU-GTM-02).
- Presupuesto de referencia: supuesto S-03 (~USD 250/mes de infraestructura).

> **Variante de un nodo.** D-19 fija dos nodos. Si el dueño decide empezar con uno, los pasos son
> los mismos con `prod-data` = `prod-app`: Postgres y Redis sin mapeo de puertos (la API los
> alcanza por la red `coolify` usando el UUID del recurso como host), sin red privada y sin
> reglas de firewall para 5432 y 6379.

### 1.2 Bucket S3 compatible (lo usa HU-ARCH-03)

- En **otro proveedor** (o al menos otra región) que el VPS: es el respaldo fuera del sitio.
- Privado, con versionado (y bloqueo de objetos si el proveedor lo ofrece).
- Una llave de acceso que solo pueda escribir y leer ese bucket. Va al gestor de contraseñas; no
  se usa en esta HU.

### 1.3 DNS

- La zona `solvershub.site` ya está en Cloudflare (HU-GTM-01). No se crea nada a mano: las rutas
  del túnel crean los CNAME (fase 6).
- Anotar a dónde apuntan hoy `solvershub.site` y `www.solvershub.site`. Si apuntan a otro hosting,
  ese registro se reemplaza en la fase 6.
- **No tocar** MX, SPF, DKIM, DMARC ni los demás registros del correo.

### 1.4 Túnel y token

1. Zero Trust → Networks → Tunnels → **Create a tunnel** → Cloudflared → nombre `solvershub-prod`.
2. Copiar el token del comando de instalación (empieza con `eyJ`). Va al gestor de contraseñas;
   nunca al repo, a Coolify ni a un chat.
3. Todavía **no** agregar rutas.

### 1.5 Cuentas y llaves de producción

Cada valor va al gestor de contraseñas. Los nombres de las variables están en
`docs/despliegue-coolify.md` (Variables por recurso).

- [ ] LLM: llave de producción (DeepSeek) con tope de gasto, y la de OpenAI (respaldo).
- [ ] Sentry: proyecto de producción → DSN.
- [ ] Servicio OTLP con plan gratuito (p. ej. Grafana Cloud): URL de trazas y credencial.
- [ ] Meta: app de producción, distinta de la de test (HU-ARCH-02).
- [ ] Wompi: llaves de producción del cobro de la plataforma, si `BILLING_PROVIDER=wompi`.
- [ ] SMTP del correo de sistema.
- [ ] GitHub: un token *classic* con **solo** `read:packages` (y vencimiento) para que `prod-data`
      baje la imagen de Postgres de GHCR (4.1), salvo que el dueño haga público ese paquete.
- [ ] Copia de las variables de la heredada que se reutilizan (origen **heredada** en la tabla):
      se leen en el Coolify local, en la app `ggzjdyc7zsemt6kzafx5rg7m`.
- [ ] Secretos nuevos: `APP_SECRET_KEY` (`openssl rand -hex 32`) y una clave nueva para
      `TOKEN_MASTER_KEYS` (`python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"`).

## 2. Operador: preparar los servidores

### 2.1 Firewall del proveedor

| Nodo | Entrante permitido | Todo lo demás |
| --- | --- | --- |
| `prod-app` | TCP 22 (si se puede, solo desde la IP del operador) | cerrado |
| `prod-data` | TCP 22 desde `<ip-privada-app>` (Coolify administra el nodo por SSH) y desde el operador; TCP 5432 y 6379 solo desde la red privada | cerrado |

La salida queda abierta (GitHub, registros de imágenes, Cloudflare, APIs de Meta, del LLM y de
Wompi). En muchos proveedores el firewall solo filtra la interfaz pública: la red privada no
pasa por él, y eso es lo esperado.

### 2.2 SSH y sistema

- Solo llave (`PasswordAuthentication no`) y actualizaciones automáticas de seguridad
  (`unattended-upgrades`).
- Coolify entra por SSH a los nodos que administra, incluido `prod-app` desde su propia red de
  Docker: si se restringe el SSH por IP, permitir también las redes de Docker (documentación de
  Coolify, "Firewall").
- Red privada: anotar las IP (`ip -4 addr`) y probar `ping -c 3 <ip-privada-data>` desde
  `prod-app`.

## 3. Operador: Coolify

1. Instalar en `prod-app`:
   ```bash
   curl -fsSL https://cdn.coollabs.io/coolify/install.sh | sudo bash
   ```
   El instalador deja Docker con rotación de logs (`json-file`, 10 MB × 3).
2. Entrar a la UI por túnel SSH, desde la máquina del operador. Se usa el puerto local `8100`
   porque el `8000` es el Coolify de test:
   ```bash
   ssh -N -L 127.0.0.1:8100:127.0.0.1:8000 -L 127.0.0.1:6001:127.0.0.1:6001 \
       -L 127.0.0.1:6002:127.0.0.1:6002 root@<ip-publica-app>
   ```
   Abrir `http://127.0.0.1:8100`. Si los puertos `6001`/`6002` ya están ocupados en la máquina
   local, quitar esos dos `-L`: la UI funciona igual, sin actualizaciones en vivo.
3. Crear la cuenta de administrador (el dueño), con contraseña fuerte y segundo factor.
4. Settings: desactivar la actualización automática de Coolify (se actualiza a propósito, fuera
   de horario).
5. Servers → **Add** `prod-data`: IP `<ip-privada-data>`, usuario `root`, la llave privada que
   genera Coolify (su pública va a `/root/.ssh/authorized_keys` de `prod-data`) → Validate (instala
   Docker).
6. En `prod-data`, comprobar la rotación de logs: `cat /etc/docker/daemon.json`. Si no tiene
   `log-opts`, copiar el de `prod-app` y `systemctl restart docker` (antes de crear recursos).
7. Proyecto **SolversHub** con el entorno `production`.
8. Fuente del código: una GitHub App con acceso solo a `agent-core`, `solvershub-panel` y
   `solvershub-landing`, o una deploy key de solo lectura por repo. **Auto Deploy desactivado**
   en todas las apps: producción se despliega con la skill `release`.
9. Settings → Advanced → API access, y un token de API para la skill `release` (al gestor de
   contraseñas).
10. El proxy de Coolify (Traefik) no se usa: el túnel llega directo a los puertos de loopback.
    Puede quedar detenido (Servers → `prod-app` → Proxy).

## 4. Operador: datos en `prod-data`

### 4.1 Imagen de Postgres

Coolify hace `pull` de la imagen en cada arranque: tiene que estar en un registry. La imagen es
`ghcr.io/devrochy/solvershub-postgres:pg17-postgis`, la que publica `ci-postgres-image.yml` desde
`docker/postgres/Dockerfile` y usa el CI (ADR-0120): producción corre lo mismo que se prueba. En
`prod-data`, como `root` (Coolify ejecuta Docker como `root`):

```bash
read -rs GHCR_TOKEN   # el token read:packages de 1.5; no se ve ni queda en el historial
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u devrochy --password-stdin && unset GHCR_TOKEN
docker pull ghcr.io/devrochy/solvershub-postgres:pg17-postgis
```

El token queda en `/root/.docker/config.json`: por eso solo `read:packages` y con vencimiento.
Cuando vence, el `pull` falla y Coolify no puede reiniciar la base (la imagen tiene que ser
"pullable"): renovarlo antes y anotar la fecha. Si el paquete es público, no hace falta el `login`. Alternativa sin GHCR (la de test): construir
la imagen en `prod-data` y servirla desde un registry local, como en
`docs/despliegue-coolify.md` (Gotchas, PostGIS).

### 4.2 Recurso Postgres

+ New → Database → PostgreSQL, en el servidor `prod-data`:

- Nombre `agent-core-postgres`; imagen `ghcr.io/devrochy/solvershub-postgres:pg17-postgis`.
- Usuario `postgres`, base inicial `agent_core`, contraseña generada por Coolify (al gestor de
  contraseñas).
- Ports Mappings: `<ip-privada-data>:5432:5432`. Si Coolify no acepta la IP, `5432:5432`: el
  firewall del proveedor cierra el puerto público. **No** activar "Make it publicly available".
- Start. Comprobar: `docker exec <uuid-postgres> psql -U postgres -d agent_core -tAc "select version()"`.

### 4.3 Roles, extensiones, dueño y marca

Como superusuario. `\password` pide la clave sin dejarla en el historial; se generan dos claves
nuevas (al gestor de contraseñas). Comprobado contra todas las migraciones del core y de los packs
el 2026-10-09:

```bash
docker exec -it <uuid-postgres> psql -U postgres -d agent_core
```

```sql
-- Dueño de las tablas: corre las migraciones. BYPASSRLS porque la 0034 y la 0052 lo exigen;
-- sin superusuario, sin CREATEROLE ni CREATEDB.
CREATE ROLE agent_core_migrations LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE;
\password agent_core_migrations
-- Runtime (API, worker y beat): RLS lo frena. Creado antes de migrar, la 0003 no le pone la
-- clave de desarrollo.
CREATE ROLE app_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
\password app_runtime
ALTER DATABASE agent_core OWNER TO agent_core_migrations;
-- vector y postgis exigen superusuario; las migraciones usan IF NOT EXISTS.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;
-- Marca de la base nueva: es lo único que habilita scripts/vaciar_base_nueva.sql.
COMMENT ON DATABASE agent_core IS 'solvershub-produccion-vps';
\q
```

Con esas claves se arman `MIGRATIONS_DATABASE_URL` (solo la API) y `DATABASE_URL` (API, worker y
beat). Si una clave tiene caracteres especiales (`@ : / ? # %`), va codificada en la URL.

### 4.4 Recurso Redis

+ New → Database → Redis, en `prod-data`: nombre `agent-core-redis`, imagen `redis:7.2`, contraseña
generada por Coolify, Ports Mappings `<ip-privada-data>:6379:6379` (mismo criterio que Postgres).
Persistencia: AOF activo (`appendonly yes`, `appendfsync everysec` en la configuración propia del
recurso); ahí viven la cola de entrada y la de Celery. Comprobar con `docker exec -it
<uuid-redis> redis-cli`, `AUTH <clave>` y `CONFIG GET appendonly`.

### 4.5 Red

```bash
# desde prod-app: tiene que conectar
nc -zv <ip-privada-data> 5432 && nc -zv <ip-privada-data> 6379
# desde fuera del VPS (la máquina del operador): tiene que fallar
nc -zv -w 5 <ip-publica-data> 5432; nc -zv -w 5 <ip-publica-data> 6379
nc -zv -w 5 <ip-publica-app> 8000; nc -zv -w 5 <ip-publica-app> 3003
```

## 5. Operador: aplicaciones en `prod-app`

Variables de cada recurso: tabla de `docs/despliegue-coolify.md`. `APP_ENV` va marcada *Not
available during build*.

### 5.1 API

+ New → Application → repo privado `devrochy/agent-core`, rama `main`, build pack Dockerfile
(`/Dockerfile`, base `/`), servidor `prod-app`:

- Nombre `agent-core-api`. Ports Exposes `8000`; Ports Mappings `127.0.0.1:3003:8000` (en la UI);
  Network Aliases `agent-core-api`; sin dominios.
- Health check HTTP: `/healthz`, puerto `8000`.
- Persistent Storage: volumen `agent-core-tenants` en `/app/tenants`.
- Variables: las de la columna A, **incluida** `MIGRATIONS_DATABASE_URL`, y `AGENT_CORE_ROLE=api`.
- Deploy. En los logs: `[entrypoint] aplicando migraciones`, `✓ Migraciones del core aplicadas`,
  una línea `✓` por pack, y el contenedor `healthy`. Así queda la **base nueva migrada desde
  cero**.

### 5.2 Dueño del volumen (si el Dockerfile no crea `/app/tenants`)

```bash
API=$(docker ps --format '{{.Names}}' | grep <uuid-api> | head -1)
docker exec -u root "$API" chown -R app:app /app/tenants
docker exec "$API" sh -c 'touch /app/tenants/.prueba && rm /app/tenants/.prueba && echo escribe'
```

### 5.3 Aislamiento en la base nueva (criterio "Aislamiento verificable")

```bash
docker exec "$API" python scripts/verificar_aislamiento.py | tee aislamiento-base-nueva.txt
```

Esperado: todas las líneas en `[OK]` y código 0. `rls_forced` es el check del checklist de
producción; `rol_runtime` y `rol_esperado` prueban que el runtime conecta con `app_runtime` y no
es dueño de nada; `contrasena_runtime`, que no usa la clave de desarrollo. Guardar la salida con
fecha: es evidencia de la HU.

### 5.4 Worker y beat

Cuando la API está `healthy`, dos apps más del mismo repo, rama y Dockerfile:

| | `agent-core-worker` | `agent-core-beat` |
| --- | --- | --- |
| Custom Docker Options | `--entrypoint /app/scripts/entrypoint-worker.sh` | `--entrypoint /app/scripts/entrypoint-beat.sh` |
| Puertos | sin Ports Mappings | sin Ports Mappings |
| Health check | desactivado | desactivado |
| Persistent Storage | `agent-core-tenants` en `/app/tenants` | `agent-core-tenants` en `/app/tenants` |
| Variables | columna W: **sin** `MIGRATIONS_DATABASE_URL`; `AGENT_CORE_ROLE=worker`; `OTEL_SERVICE_NAME=agent-core-worker` | columna B: **sin** `MIGRATIONS_DATABASE_URL`; `AGENT_CORE_ROLE=beat`; `OTEL_SERVICE_NAME=agent-core-beat` |

Logs esperados: `celery@… ready` en el worker y `beat: Starting...` en el beat. A los dos minutos
el latido tiene que estar: `docker exec "$API" python scripts/verify_production.py` muestra
`[OK] worker_heartbeat_and_dlq` (los checks de Meta pueden fallar todavía: el borde no está).

Los tres contenedores comparten el volumen:

```bash
for u in <uuid-api> <uuid-worker> <uuid-beat>; do
  docker inspect --format '{{range .Mounts}}{{.Name}} -> {{.Destination}}{{"\n"}}{{end}}' \
    "$(docker ps --format '{{.Names}}' | grep "$u" | head -1)"
done   # el mismo nombre de volumen en los tres
```

### 5.5 Panel

+ New → Application → `devrochy/solvershub-panel`, rama `main`, base `/apps/panel-web`, Dockerfile
`/Dockerfile`: nombre `panel-web`; Ports Exposes `3000`; Ports Mappings `127.0.0.1:3004:3000`;
health check HTTP `/login`, puerto `3000`; variable `AGENT_CORE_API_URL=http://agent-core-api:8000`.

### 5.6 Landing

+ New → Application → `devrochy/solvershub-landing`, rama `main`, Dockerfile `/Dockerfile`: nombre
`landing`; Ports Exposes `3000`; Ports Mappings `127.0.0.1:3005:3000`; health check de Coolify
**desactivado** (la imagen no trae `curl` ni `wget`; rige su `HEALTHCHECK` con `node`). Las
`NEXT_PUBLIC_*` como variables de build; `LEAD_WEBHOOK_*` y `WEBCHAT_API_BASE` de runtime.

### 5.7 Prueba sin tráfico público

Desde la máquina del operador (puertos locales `1300x` para no chocar con la heredada, que usa
`3003` en la máquina de desarrollo):

```bash
ssh -N -L 127.0.0.1:13003:127.0.0.1:3003 -L 127.0.0.1:13004:127.0.0.1:3004 \
    -L 127.0.0.1:13005:127.0.0.1:3005 root@<ip-publica-app> &
curl -s http://127.0.0.1:13003/healthz                                  # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:13004/login   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:13005/        # 200
```

> **Antes de copiar datos reales, las tres apps del motor se detienen.** Al arrancar, la API
> reprograma las conversaciones abiertas cuyo último mensaje es entrante
> (`core/orchestrator/debounce.py:188-245`) y el beat dispara los crons: con datos reales y la
> heredada atendiendo, los negocios recibirían respuestas y cobros duplicados.

## 6. Operador y dueño: borde, sin tocar `app.solvershub.site`

1. **cloudflared** en `prod-app`: Zero Trust → el túnel `solvershub-prod` → instalar el conector;
   copiar los comandos que muestra para Debian/Ubuntu (repositorio apt de Cloudflare y
   `cloudflared service install <token>`). Comprobar `systemctl is-active cloudflared` y el
   conector `HEALTHY` en Zero Trust.
2. **Rutas** (*Published application routes*) del túnel nuevo, en este orden: las de
   `api.solvershub.site` (`^/webhooks/` y `^/healthz$` → `http://localhost:3003`), luego
   `solvershub.site` y `www.solvershub.site` → `http://localhost:3005`. Las de
   `app.solvershub.site` se agregan **en el corte** (fase 8). Tabla completa en
   `docs/despliegue-coolify.md` (Borde).
3. **Access** (dueño): aplicación `api.solvershub.site` con Allow (equipo), y
   `api.solvershub.site/webhooks` y `api.solvershub.site/healthz` con Bypass.
4. Comprobar desde fuera:

   ```bash
   curl -s https://api.solvershub.site/healthz                                         # {"status":"ok"}
   curl -s -o /dev/null -w '%{http_code}\n' https://api.solvershub.site/api/v1/auth/login   # 302 (Access) o 404: nunca la API
   curl -s -o /dev/null -w '%{http_code}\n' https://www.solvershub.site/                    # 200
   curl -svI https://www.solvershub.site/ 2>&1 | grep -E 'subject:|issuer:|expire date'     # certificado válido
   ```

## 7. Ensayo del corte (recomendado)

Los pasos 8.5 a 8.8 con una copia en caliente de la heredada, sin congelarla ni mover rutas.
Sirve para medir los tiempos de la ventana y encontrar errores antes del día del corte.

1. En Coolify de producción, **detener** `agent-core-api`, `agent-core-worker` y `agent-core-beat`
   (ver el aviso de 5.7).
2. Copia en caliente (`pg_dump` toma una foto consistente sin bloquear a la heredada):
   ```bash
   docker exec huvhzaxim2notqkcxqa5930a pg_dump -U postgres -Fc -f /tmp/ensayo.dump agent_core
   docker cp huvhzaxim2notqkcxqa5930a:/tmp/ensayo.dump ~/corte/ && docker exec huvhzaxim2notqkcxqa5930a rm /tmp/ensayo.dump
   ```
3. Pasos 8.6 y 8.7 con ese archivo, y 8.8 contra la heredada: va a haber diferencias en las tablas
   que la heredada siguió escribiendo después de la copia; las demás tienen que coincidir.
4. **No arrancar** las apps del motor con esos datos. Vaciar la base otra vez
   (`vaciar_base_nueva.sql`, paso 8.7) y borrar el archivo del ensayo de los dos lados.
5. Anotar cuánto tardó cada paso: define la ventana.

Después del ensayo la base nueva queda sin filas, también en `alembic_version*`: la API no
arranca (su entrypoint intentaría migrar sobre tablas que ya existen) hasta que la restauración
del corte traiga las versiones. Es intencional: nada del motor corre entre el ensayo y el corte.
Si hace falta volver a tenerla arriba antes, se recrea la base (fase 4.3 sin los roles) y se
redespliega la API.

## 8. Corte (ventana de mantenimiento)

### Antes de la ventana

- [ ] Ventana elegida **fuera de los crons diarios** (todos en UTC: 02:00, 02:10, 03:15, 03:45,
      04:00, 09:00, 09:30, 14:00, 15:30, 20:30, 23:55; ver `core/tasks/celery_app.py` y los
      `@register_cron` de los packs). SUPUESTO A VALIDAR: entre 04:30 y 08:30 UTC (23:30 a 03:30
      en Colombia). Los crons horarios que caigan en la ventana corren a la hora siguiente.
- [ ] Aviso a los negocios, si alguno opera en ese horario.
- [ ] Mismo commit en las dos producciones: `docker exec <contenedor-api> printenv SOURCE_COMMIT`
      en la heredada y en la nueva.
- [ ] Mensajes fallidos (DLQ) de la heredada vacíos o con una decisión tomada (panel → Mensajes
      fallidos).
- [ ] Cuántas conversaciones va a retomar la API nueva al arrancar (abiertas, con el bot activo y
      el último mensaje entrante). En la heredada:
      ```bash
      docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -d agent_core -tAc "
        select count(*) from (
          select distinct on (m.conversation_id) m.direction
          from messages m join conversations c on c.id = m.conversation_id
          where c.status = 'open' and c.bot_paused = false
          order by m.conversation_id, m.created_at desc) x
        where x.direction = 'inbound';"
      ```
      Si no es 0, el asistente les va a contestar al arrancar (igual que en un reinicio de la
      heredada). Decidir si se aceptan o se cierran antes.
- [ ] En la máquina de desarrollo: un checkout del repo con el venv, la carpeta `~/corte` y
      `scripts/vaciar_base_nueva.sql` copiado a `prod-data` (`/root/corte/`).

### 8.1 Congelar la entrada (T0)

Cloudflare → `solvershub.site` → Security → WAF → Custom rules → regla `corte-produccion`:

```
(http.host eq "app.solvershub.site" and starts_with(http.request.uri.path, "/webhooks/"))
```

Acción **Block**. Meta reintenta las entregas que no reciben 200, con frecuencia decreciente,
hasta por 7 días (documentación de webhooks de WhatsApp), y el motor descarta los duplicados
(`processed_events`). Wompi reintenta solo hasta 3 veces más (skill `pagos-wompi`): los eventos de
la ventana se concilian al final (8.13). Anotar la hora.

### 8.2 Drenar las colas de la heredada

Esperar unos 5 minutos (debounce y presupuesto del turno) y revisar:

```bash
API_VIEJA=$(docker ps -q -f name=ggzjdyc7zsemt6kzafx5rg7m | head -1)
docker exec -i "$API_VIEJA" python - <<'PY'
import os

import redis

r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
try:
    ultimo = r.xinfo_stream("agent_core:inbound")["last-generated-id"]
    grupos = r.xinfo_groups("agent_core:inbound")
except redis.ResponseError:
    ultimo, grupos = "-", []  # el stream no existe: no hay nada en vuelo
for g in grupos:
    print(f"entrada {g['name']}: pendientes={g['pending']} sin_leer={g.get('lag')} "
          f"(último entregado {g['last-delivered-id']}, último del stream {ultimo})")
print("debounce:", r.zcard("debounce:pending"))
print("celery en cola:", r.llen("celery"), "| en curso:", r.hlen("unacked"))
print("DLQ:", r.xlen("agent_core:inbound:dlq"))
PY
```

Seguir solo con `pendientes=0`, `sin_leer=0` (o el último entregado igual al último del stream),
`debounce: 0` y la cola de Celery en `0 | 0`. Redis no se migra: lo que queda en él es caché o
memoria de trabajo, que se rearma desde Postgres.

### 8.3 Copiar `TENANTS_ROOT` de la heredada

```bash
mkdir -p ~/corte
docker exec "$API_VIEJA" tar -C /app/tenants -cf - . > ~/corte/tenants-corte.tar
docker exec "$API_VIEJA" sh -c 'cd /app/tenants && find . -type f | wc -l'
docker exec "$API_VIEJA" sh -c 'cd /app/tenants && find . -type f -exec sha256sum {} + | sort -k2 | sha256sum'
```

Anotar el número de archivos y el hash.

### 8.4 Detener los que escriben

- Coolify local (heredada): detener la app `ggzjdyc7zsemt6kzafx5rg7m`, el worker
  `wv6kycjeae0lmfj11dprkasq` y el beat `hwh7s5gscnn1k3yqf88xceja`. Postgres sigue arriba.
- Coolify de producción: detener `agent-core-api`, `agent-core-worker` y `agent-core-beat` (si
  quedaron corriendo).

Anotar la hora: desde acá, nada escribe en ninguna de las dos bases.

### 8.5 Copia de la base heredada

```bash
docker exec huvhzaxim2notqkcxqa5930a pg_dump -U postgres -Fc -f /tmp/agent_core-corte.dump agent_core
docker cp huvhzaxim2notqkcxqa5930a:/tmp/agent_core-corte.dump ~/corte/
docker exec huvhzaxim2notqkcxqa5930a rm /tmp/agent_core-corte.dump
sha256sum ~/corte/agent_core-corte.dump
```

Los roles **no** se copian (`pg_dumpall --roles-only` no se usa): el VPS tiene sus propios roles
con claves nuevas (criterio "Recursos y secretos"), y el esquema y sus dueños los crearon las
migraciones (5.1). De la heredada solo se copian las filas.

### 8.6 Llevar las copias al VPS

```bash
scp ~/corte/agent_core-corte.dump root@<ip-publica-data>:/root/corte/
scp ~/corte/tenants-corte.tar root@<ip-publica-app>:/root/corte/
ssh root@<ip-publica-data> sha256sum /root/corte/agent_core-corte.dump   # el mismo hash de 8.5
```

### 8.7 Vaciar la base nueva y restaurar las filas

En `prod-data`:

```bash
PG=<uuid-postgres>
docker exec -i "$PG" psql -U postgres -d agent_core -v ON_ERROR_STOP=1 < /root/corte/vaciar_base_nueva.sql
#   NOTICE:  Base agent_core vaciada: N tablas sin filas, esquema intacto.
docker cp /root/corte/agent_core-corte.dump "$PG":/tmp/
docker exec "$PG" pg_restore -U postgres -d agent_core --data-only --disable-triggers \
  --single-transaction --no-owner --exit-on-error /tmp/agent_core-corte.dump
docker exec "$PG" psql -U postgres -d agent_core -c 'ANALYZE;'
docker exec "$PG" rm /tmp/agent_core-corte.dump
```

- `vaciar_base_nueva.sql` borra las filas que pusieron las migraciones y el arranque de la API
  (`tenant-1`, planes, tipos de conexión, registro de plugins), que chocarían con las de la
  heredada. Solo corre en la base con la marca de 4.3.
- `--disable-triggers` (exige superusuario) carga sin disparar triggers ni validar las FK
  durante la carga; `--single-transaction`: todo o nada. Si las columnas de las dos bases no
  coinciden, falla acá: es la señal de que el commit no es el mismo.
- Ensayado de punta a punta el 2026-10-09 en un Postgres local: 109 tablas iguales después de
  restaurar.

### 8.8 Comparar el conteo de filas (criterio "Migración de datos")

En la máquina de desarrollo, en el checkout del repo. El superusuario de cada base: el de la
heredada está en el recurso `huvhzaxim2notqkcxqa5930a` del Coolify local; el del VPS, en
`agent-core-postgres`. `read -rs` los pide sin dejarlos en el historial:

```bash
ssh -N -L 127.0.0.1:55499:<ip-privada-data>:5432 root@<ip-publica-data> &
read -rs CLAVE_ORIGEN; read -rs CLAVE_DESTINO
export ORIGEN_DATABASE_URL="postgresql://postgres:${CLAVE_ORIGEN}@127.0.0.1:5439/agent_core"
export DESTINO_DATABASE_URL="postgresql://postgres:${CLAVE_DESTINO}@127.0.0.1:55499/agent_core"
.venv/bin/python scripts/comparar_conteos.py | tee ~/corte/conteos-$(date -u +%Y%m%dT%H%MZ).txt
unset CLAVE_ORIGEN CLAVE_DESTINO ORIGEN_DATABASE_URL DESTINO_DATABASE_URL
```

Esperado: `Resumen: N tablas comparadas, N iguales, 0 con diferencias.` y código 0 (también
compara las versiones de `alembic_version*`). Con código 1 **no se sigue**: se investiga o se
vuelve atrás (fase 9, caso A).

### 8.9 Arrancar la producción nueva

1. Coolify de producción → **Deploy** de `agent-core-api`. Las migraciones no tienen nada que
   hacer (la versión de esquema viene de la heredada). En los logs, la línea
   `orphaned_conversations_recovered` (si aparece) tiene que dar el número de la revisión previa.
2. Restaurar `TENANTS_ROOT`, en `prod-app`:
   ```bash
   API=$(docker ps --format '{{.Names}}' | grep <uuid-api> | head -1)
   docker exec -i "$API" tar -C /app/tenants -xf - < /root/corte/tenants-corte.tar
   docker exec "$API" sh -c 'cd /app/tenants && find . -type f | wc -l'
   docker exec "$API" sh -c 'cd /app/tenants && find . -type f -exec sha256sum {} + | sort -k2 | sha256sum'
   ```
   El número de archivos y el hash tienen que ser los de 8.3.
3. **Deploy** de `agent-core-worker` y `agent-core-beat`.
4. Aislamiento con los datos migrados:
   `docker exec "$API" python scripts/verificar_aislamiento.py | tee ~/corte/aislamiento-corte.txt`
   → todo `[OK]`.

### 8.10 Mover `app.solvershub.site` al túnel nuevo

1. Zero Trust → Tunnels → `solvershub` (heredado) → Published application routes → borrar
   `app.solvershub.site`.
2. DNS → si quedó el CNAME `app`, borrarlo.
3. Tunnels → `solvershub-prod` → agregar, en este orden: `app.solvershub.site` `^/webhooks/` →
   `http://localhost:3003`; `^/healthz$` → `http://localhost:3003`; sin path →
   `http://localhost:3004`.
4. Las aplicaciones de Access de `app.solvershub.site` no cambian.

```bash
curl -s https://app.solvershub.site/healthz   # {"status":"ok"}, ahora desde el VPS
```

En el navegador, después de Access, `app.solvershub.site` muestra el **panel nuevo** (antes
redirigía a `/admin`, el panel clásico).

### 8.11 Abrir la entrada

Borrar la regla `corte-produccion` del WAF. Anotar la hora: los reintentos de Meta empiezan a
llegar a la producción nueva.

### 8.12 Verificar

- Webhook de Meta (el token de verificación registrado en Meta):
  ```bash
  read -rs VERIFY_TOKEN
  curl -s --get https://app.solvershub.site/webhooks/whatsapp --data-urlencode "hub.mode=subscribe" \
    --data-urlencode "hub.verify_token=${VERIFY_TOKEN}" --data-urlencode "hub.challenge=corte-ok"
  # corte-ok
  unset VERIFY_TOKEN
  ```
- **El asistente responde en cada negocio**: la lista sale de
  `docker exec <uuid-postgres> psql -U postgres -d agent_core -c "select slug, name, status from tenants where deleted_at is null order by slug;"`.
  Para cada negocio activo: panel → cambiar de negocio → **Probar asistente** → mandar "hola" →
  tiene que contestar (el playground corre el mismo loop del runtime sin canal ni persistencia).
  Captura por negocio.
- Checklist de producción del panel (`/conexiones/produccion`, o
  `docker exec "$API" python scripts/verify_production.py`): `worker_heartbeat_and_dlq` y
  `rls_forced` en verde.
- Logs de la API y del worker en Coolify sin errores nuevos; Sentry recibe eventos del entorno
  `production`.
- Si hay un número de WhatsApp conectado: un mensaje real desde un teléfono recibe respuesta (es
  también el criterio "Mensaje real" de HU-ARCH-02).

### 8.13 Cerrar la ventana

- Wompi: revisar en el panel de Wompi las transacciones aprobadas entre 8.1 y 8.11 y conciliar
  las que no hayan llegado (cobro de la plataforma y `pack_payments`).
- Avisar el fin del mantenimiento.
- La heredada queda **detenida, no borrada**, durante el periodo de vuelta atrás (SUPUESTO A
  VALIDAR: 7 días).

## 9. Vuelta atrás

- **A. Antes de 8.10** (las rutas no se movieron): arrancar en el Coolify local la app, el worker
  y el beat de la heredada; borrar la regla del WAF; detener las apps nuevas. La base heredada
  solo se leyó: no se pierde nada.
- **B. Después de 8.10 y dentro de la ventana**: quitar las rutas de `app.solvershub.site` del
  túnel nuevo (y su CNAME, si queda) y devolverla al túnel `solvershub` (`http://localhost:3003`),
  arrancar la heredada, detener la nueva y borrar la regla del WAF si sigue. Lo que la nueva
  escribió desde 8.11 no está en la heredada: listarlo (`created_at` posterior a la hora de 8.11
  en `messages`, `conversations`, `usage_ledger` y las tablas de los packs) y reconciliarlo a
  mano.
- **C. Después de la ventana**: ya no hay vuelta atrás directa. Volver a la heredada es otra
  migración, con las mismas herramientas en sentido contrario (marcar la base heredada, vaciarla,
  restaurar y comparar), decidida por el dueño. **El punto de no retorno es el cierre de la
  ventana (8.13).**

## 10. Prueba de apagado (criterio "Separación real")

Con el corte hecho. Test se cae mientras dure (vive en la máquina de desarrollo): es lo esperado.

1. Anotar fecha y hora (UTC y Colombia) y capturar la pantalla del apagado con el reloj visible:
   `date -u; date; sudo poweroff`.
2. Esperar 5 minutos (el latido del worker dura 180 s en Redis: después de 5 minutos, un latido
   vigente prueba que el beat y el worker siguen vivos sin la máquina de desarrollo).
3. Desde otro dispositivo, **fuera de la red de la oficina** (un teléfono con datos móviles),
   capturar con la hora visible:
   - `https://www.solvershub.site` y `https://solvershub.site`: la landing carga; detalle del
     certificado (emisor y vigencia).
   - `https://app.solvershub.site`: Access → login del panel nuevo → entra.
   - `https://app.solvershub.site/healthz` y `https://api.solvershub.site/healthz`:
     `{"status":"ok"}`.
   - `https://app.solvershub.site/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=apagado`:
     muestra `apagado`.
   - Panel → `/conexiones/produccion` → correr el checklist: `worker_heartbeat_and_dlq` en verde
     (worker y beat).
   - Panel → **Probar asistente** en un negocio: contesta.
4. Encender la máquina de desarrollo y anotar la hora.

## 11. Después del corte

### 11.1 PR en agent-core (Definición de Terminado de la HU)

- [ ] `docs/despliegue-coolify.md`: los UUID reales en lugar de `<uuid-…>`, el UUID del túnel
      `solvershub-prod`, el estado de la producción, y quitar la sección "Producción heredada" al
      terminar el periodo de vuelta atrás.
- [ ] `.claude/hooks/guards.py`: `PROD_RESOURCES` con los UUID nuevos de API, worker, beat,
      panel, landing, Postgres y Redis; `PROD_DB_PORTS` con el puerto local del túnel a la base de
      producción (`55499`, el de 8.8) en lugar de `5439` y `6381`; `PROD_HOST` sigue siendo
      `app.solvershub.site`, y conviene cubrir también `api.solvershub.site`. Actualizar el
      comentario de arriba de `PROD_RESOURCES` (dice que la app de producción se llama
      `agent-core-test` en Coolify).
- [ ] `tests/test_guards_hook.py`: los casos con los UUID y los puertos heredados.
- [ ] Skill `desplegar-test-agent-core`: la advertencia "Producción no se toca desde esta skill"
      pasa a decir que producción vive en otro Coolify (el de `prod-app`) y con qué UUID.
- [ ] Skill `release`: el punto 9 del checklist (backup) y los pasos 3 y 4 de "Despliegue" con los
      UUID nuevos, la API de Coolify por el túnel SSH (`127.0.0.1:8100`) y los `docker` por SSH a
      `prod-app` y `prod-data`.
- [ ] Que no quede ninguna referencia operativa a la heredada:
      `grep -rn -E 'ggzjdyc|huvhzax|hwh7s5|wv6kyc|gk9bag|442da5f7' .claude docs tests`
      (los planes viejos de `docs/plans/` pueden quedar como historia).
- [ ] Fragmento en `changelog.d/`.

### 11.2 Panel y landing

- [ ] Panel: `AGENT_CORE_API_URL=http://agent-core-api:8000` en el Coolify de producción. El README
      del panel ya remite a `docs/despliegue-coolify.md`.
- [ ] Landing: `NEXT_PUBLIC_PANEL_URL=https://app.solvershub.site` (variable de **build**: si
      cambia, se reconstruye) y `NEXT_PUBLIC_SITE_URL`. Comprobar el botón "Iniciar sesión" de la
      landing en producción.

### 11.3 Retirar la heredada (al terminar el periodo de vuelta atrás)

- [ ] Último respaldo de la base y de `TENANTS_ROOT` heredados, cifrado y fuera de la máquina.
- [ ] En el Coolify local, borrar el entorno `production` (app, worker, beat, Postgres, Redis y
      sus volúmenes).
- [ ] Borrar el túnel `solvershub` (`442da5f7-…`) en Zero Trust; en la máquina de desarrollo,
      `~/.cloudflared/run-solvershub.sh` y su línea `@reboot` del crontab.
- [ ] Rotar lo que vivió en la máquina de desarrollo y se copió al VPS: llaves del LLM y de Wompi
      si son las mismas, y los secretos de Meta (coordinado con HU-ARCH-02). La clave vieja de
      `TOKEN_MASTER_KEYS` se queda mientras haya filas cifradas con ella.
- [ ] Comparar los hashes de los secretos de producción y de test (`docs/despliegue-coolify.md`,
      Separación de test) y guardar el resultado.

### 11.4 Evidencia para cerrar la HU

| Criterio | Evidencia |
| --- | --- |
| Separación real | capturas de la fase 10 con hora |
| Panel y landing en producción | capturas de 10.3 y el commit de `main` desplegado en Coolify (panel y landing) |
| API privada | salida de los `curl` de la fase 6 (`/api/v1` → 302 o 404) y de `nc` de 4.5 |
| Migración de datos | `conteos-*.txt` de 8.8 y las capturas de **Probar asistente** por negocio (8.12) |
| Recursos y secretos | tabla de hashes de 11.3 y la lista de recursos con sus UUID |
| Aislamiento verificable | `aislamiento-base-nueva.txt` (5.3) y `aislamiento-corte.txt` (8.9) |

## 12. Lo que queda para otras HU

- **HU-ARCH-02 (webhook de Meta en producción)**: verificar el webhook desde la app de Meta de
  producción (URL y token), suscripción de la app, un mensaje real, y decidir si las URL
  registradas pasan a `api.solvershub.site` (con `PUBLIC_BASE_URL`) o siguen en `app.`. Si pasan,
  actualizar Meta (por WABA) y la URL de eventos de Wompi (una por entorno).
- **HU-ARCH-03 (backups WAL/PITR fuera del sitio y restauración probada)**. Solo el esquema del
  plan; no se implementa acá:
  - WAL continuo y base backup diario de `agent-core-postgres` al bucket de 1.2, con wal-g o
    pgBackRest (el binario se agrega a la imagen de `docker/postgres/`; `archive_command` y
    `archive_timeout` para no pasar de 15 minutos de RPO, D-20).
  - Retención: 30 días de PITR y 12 mensuales (política del bucket y de la herramienta).
  - `TENANTS_ROOT` (con las evidencias de cobranza): copia diaria versionada al mismo bucket.
  - Restauración probada en un servidor limpio, cronometrada contra el RTO de 4 horas, con
    evidencia en `solvershub-comercial/runbooks/evidencia/`; se repite cada trimestre.
  - Alerta si el envío de WAL falla más de 15 minutos (con HU-ARCH-07).
  - Mientras tanto: respaldo lógico diario de la base al bucket (Scheduled Backups del recurso en
    Coolify, `pg_dump`) y el `tar` del volumen. No cumple el RPO de 15 minutos.
- **HU-ARCH-07**: `/healthz` profundo, alertas, tablero y Sentry del panel.
- **HU-ARCH-08 y SPEC-25**: rutas públicas para la APK de cobranza y la app oficial.
- **HU-REP-06**: cuando exista `solvershub-infra`, la topología y las reglas del túnel se mudan
  allá.

## Decisiones pendientes antes del corte

- **Access del panel**: con la política Allow del equipo, los negocios clientes no pueden entrar a
  `app.solvershub.site`. Si deben entrar, cambiar esa política antes de anunciar el panel.
- **`PUBLIC_BASE_URL`**: se queda en `app.solvershub.site` para no romper los registros de Meta y
  Wompi; `api.solvershub.site` queda lista para HU-ARCH-02.
- **Panel clásico (`/admin` del motor)**: no tiene ruta pública en producción. Si el master lo
  necesita durante la transición, entra por túnel SSH (`ssh -L 127.0.0.1:13003:127.0.0.1:3003
  root@<ip-publica-app>` y `http://127.0.0.1:13003/admin`).
- **`meta_private.meta_tokens` sin FORCE**: ver la fase 0.

## Registro de la ejecución

| Paso | Hora (UTC) | Quién | Resultado / archivo de evidencia |
| --- | --- | --- | --- |
| 5.3 Aislamiento en la base nueva | | | |
| 7 Ensayo (duración de 8.5 a 8.8) | | | |
| 8.1 Entrada congelada (T0) | | | |
| 8.4 Escritura detenida | | | |
| 8.7 Restauración terminada | | | |
| 8.8 Conteos iguales | | | |
| 8.10 `app.solvershub.site` en el VPS | | | |
| 8.11 Entrada abierta | | | |
| 10 Apagado / encendido | | | |
