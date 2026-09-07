# Manual de Instalación

De cero a plataforma funcionando. Tiempo estimado: 10 minutos.

agent-core es multi-tenant: **se instala una vez por despliegue** y luego cada
cliente se da de alta como tenant (`agent-core tenant create` o el panel).
Ver [MULTITENANT.md](MULTITENANT.md).

## Pre-requisitos

| Software | Versión mínima | Verificar con |
|---|---|---|
| Python | 3.12 | `python --version` |
| Docker + Docker Compose | 24.x | `docker compose version` |
| Git | 2.x | `git --version` |

## 1. Clonar e instalar el CLI

```bash
git clone <url-del-repo> mi-despliegue
cd mi-despliegue
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

`python3` en vez de `python`: en muchas distribuciones el ejecutable `python`
sencillamente no existe. Dentro del venv ya da igual — `python`, `python3`, `pip` y
`pip3` apuntan todos al mismo intérprete.

Verifica que el CLI es el de este proyecto, no otro instalado antes:

```bash
command -v agent-core     # debe estar dentro de .venv/bin/
agent-core --help
```

> Si `agent-core` resuelve a `~/.local/bin/agent-core`, estás ejecutando el CLI de
> otra instalación. Activa el venv, o desinstala el global con `pip uninstall agent-core`.

## 2. Bootstrap del despliegue

```bash
agent-core init
```

`init` prepara el despliegue: genera `.env`, levanta PostgreSQL y Redis, aplica
las migraciones y crea el admin master. Después de esto la plataforma está lista
para dar de alta clientes.

**Qué resuelve solo:**

- **Puertos.** Busca puertos publicables, saltando los que ocupa otro proceso o contenedor.
- **Identidad Docker.** Escribe `COMPOSE_PROJECT_NAME`, que da a este despliegue sus
  propios contenedores y su propio volumen de datos. Dos despliegues que se llamen
  igual no comparten base de datos.
- **Re-ejecución.** Con `--force` conserva los puertos que ya tenía este despliegue.

Para automatizarlo (CI, scripts, replicar el despliegue):

```bash
agent-core init --non-interactive --name "Mi Plataforma" --llm deepseek
agent-core init --help    # todas las banderas
```

## 3. Arrancar la plataforma

```bash
# Desarrollo (recarga automática):
make dev            # = uvicorn core.main:app --reload --port 8000

# O todo el stack en Docker (API + celery worker + beat):
docker compose up -d --build
```

Verifica: `curl http://localhost:8000/healthz` → `{"status":"ok"}`

> **Importante (desarrollo):** `uvicorn --reload` solo recarga cambios en archivos
> `.py`, **NO** recarga el `.env`. Si editas el `.env` mientras el servidor corre,
> reinícialo manualmente (`Ctrl+C` + `make dev`).

## 4. Entrar al panel

Abre **http://localhost:8000/admin** e ingresa con el email/contraseña del admin
creado por `init`. Verás el Dashboard con las secciones del core: Conversaciones,
Escalaciones, Conexiones, Plugins, Costos LLM y (para admins master) **Tenants**.

## 5. Dar de alta un cliente (tenant)

Dos formas equivalentes:

**A. CLI (wizard paso a paso):**
```bash
agent-core tenant create
# 1. Nombre del negocio · slug · descripción · idioma · tono
# 2. Packs a habilitar (checkbox)
# 3. Triggers de escalación
# 4. WhatsApp (opcional, credenciales cifradas)
# 5. Revisión y crear
```

**B. Panel:** `/admin/tenants` → **+ Nuevo tenant** → el mismo wizard en 5 pasos.

Al terminar, el tenant aparece en el selector del panel y su `AgentSpec` es
editable en `/admin/spec`. Si el cliente tiene datos (catálogo, servicios):

```bash
# copia/crea TENANTS_ROOT/<slug>/catalog.json y luego:
agent-core tenant seed <slug>
```

## 6. Conectar WhatsApp (Meta Cloud API)

Por tenant, en **/admin/conexiones** (o dentro del wizard de alta): completa
`label`, `phone_number_id`, `app_secret`, `access_token` y `verify_token`. Las
credenciales se guardan cifradas (Fernet) y se **validan contra la Graph API de
Meta antes de guardar**.

**Cada tenant tiene su propio webhook**: `https://<tu-dominio>/webhooks/whatsapp/<slug>`
(lo imprime `agent-core tenant webhook <slug>` y el panel en `/admin/conexiones`).

### Prueba end-to-end real (paso a paso)

**1. Crea la WABA de prueba en Meta (gratis):**
- [business.facebook.com](https://business.facebook.com) → Business settings →
  WhatsApp → Add phone number → elige *"usar un número de prueba"* (test number).
- Crea una app en [developers.facebook.com](https://developers.facebook.com)
  (tipo Business), agrega el producto **WhatsApp** y anota:
  **Phone Number ID**, **App Secret**, **Access Token** (WhatsApp → API Setup →
  Generate token; 24 h en dev).
- WhatsApp → Recipients: agrega tu celular como **tester** (para poder chatear).

**2. Expón tu webhook con un túnel:**
```bash
cloudflared tunnel --url http://localhost:8001
# → https://<aleatorio>.trycloudflare.com   (déjalo corriendo)
```
Pon esa URL pública en el `.env` (`PUBLIC_BASE_URL=https://<aleatorio>.trycloudflare.com`)
y reinicia la API. El webhook del tenant será
`https://<aleatorio>.trycloudflare.com/webhooks/whatsapp/<slug>`.

**3. Conecta WhatsApp del tenant** en `/admin/conexiones` (Phone Number ID, App
Secret, Access Token y un Verify Token propio, ej. `miToken123`). El core valida
contra Meta antes de guardar.

**4. Registra el webhook en Meta:** tu app → **WhatsApp → Configuration**:
- **Callback URL**: `https://<aleatorio>.trycloudflare.com/webhooks/whatsapp/<slug>`
- **Verify token**: el mismo del paso 3
- Suscríbete al campo **`messages`**.

Meta hace el handshake (`GET /webhooks/whatsapp/<slug>?hub.mode=subscribe...`);
el core responde 200 + challenge si el token coincide.

**5. Prueba:** escribe desde tu celular (tester) al número de prueba. La firma se
valida (App Secret) → se resuelve el tenant por el slug → se procesa y responde.
Verás la conversación en `/admin/conversaciones`. (Con `DEMO_MODE=true` el LLM es
fake determinístico; con `LLM_API_KEY` respuestas reales.)

## 7. Instalar un pack (vertical)

```bash
agent-core plugin install pack-commerce
```

Los packs se habilitan **por tenant** desde su `AgentSpec` (checkbox en el wizard
de alta o editor `/admin/spec`). El mismo pack sirve a N clientes.

## Solución de problemas

**Empieza siempre por aquí:**

```bash
agent-core doctor
```

Revisa `.env`, Docker, puertos, conexión a PostgreSQL y Redis, migraciones y
plugins detectados. Cada fila que falla dice qué hacer. Sale con código 1 si
encuentra algo, así que sirve en scripts.

### Los fallos que más cuestan

**1. `port is already allocated` al levantar**

Otro servicio ocupó el puerto entre el `init` y el `docker compose up`. Cambia
`POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` / `APP_HOST_PORT` en el `.env`, o para
el servicio que lo ocupa.

**2. `password authentication failed` o `role "X" does not exist` al migrar**

Casi siempre es el volumen de datos. PostgreSQL aplica `POSTGRES_USER` /
`POSTGRES_PASSWORD` / `POSTGRES_DB` **solo la primera vez** que inicializa su
directorio de datos. Si el volumen `<proyecto>_pgdata` sobrevive a una instalación
anterior, conserva las credenciales viejas.

```bash
# a) Conservar los datos: pon en el .env las credenciales con las que se creó el volumen
# b) Empezar de cero (DESTRUYE los datos de ese despliegue):
docker compose down -v && agent-core init --force
```

**3. Dos despliegues se pisan**

Cada despliegue necesita su propio `COMPOSE_PROJECT_NAME` en el `.env` (lo escribe
`init`). Sin él Docker usa el nombre de la carpeta y dos clones llamados igual
comparten contenedores y volumen. `agent-core doctor` lo marca como aviso.

### Otros síntomas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `docker compose` falla con `.env not found` | No has corrido `init` | `agent-core init` genera el `.env` que compose exige |
| `required variable COMPOSE_PROJECT_NAME is missing` | `.env` heredado | `agent-core init --force` |
| `exec format error` en `docker-buildx` | Binario de buildx corrupto | `sudo apt install docker-buildx-plugin` |
| `postgres is unhealthy` | Healthcheck se autentica con el `.env` | Caso 2 de arriba: `docker compose logs postgres` |
| `MASTER_ENCRYPTION_KEY no configurada` | .env sin generar | `agent-core init` |
| Webhook devuelve 403 en el handshake | verify_token no coincide | Mismo valor en Meta y en la conexión del tenant |
| El bot no responde pero guarda mensajes | Kill-switch activo | Panel → Configuración → estado del bot |
| Plugin en estado `error` | Excepción en activación/runtime | El mensaje exacto está en /admin/plugins |
| Respuestas nunca llegan | Consumer sin arrancar | `docker compose logs core-api` |