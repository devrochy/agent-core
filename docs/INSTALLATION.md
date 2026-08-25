# Manual de Instalación

De cero a panel funcionando. Tiempo estimado: 10 minutos.

## Pre-requisitos

| Software | Versión mínima | Verificar con |
|---|---|---|
| Python | 3.12 | `python --version` |
| Docker + Docker Compose | 24.x | `docker compose version` |
| Git | 2.x | `git --version` |

## 1. Clonar e instalar el CLI

```bash
git clone <url-del-repo> mi-asistente
cd mi-asistente
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Verifica: `agent-core --help` debe listar los comandos.

## 2. Inicializar el asistente (wizard)

```bash
agent-core init
```

El wizard pregunta: nombre, negocio, idioma, tono, proveedor LLM (DeepSeek por defecto),
API key, embeddings, velocidad de respuesta, URLs de BD/Redis y el admin. Después
levanta PostgreSQL y Redis, aplica las migraciones y crea el usuario admin.

`init` va antes que `docker compose`: genera el `.env`, y compose lo exige para arrancar.

**Qué resuelve solo:**

- **Puertos.** Busca puertos publicables, saltando los que ocupa otro proceso y los que
  reclama cualquier contenedor —aunque esté parado, porque los reclamará al arrancar—.
  Te dice qué puerto eligió y quién ocupaba el que pediste.
- **Aislamiento entre asistentes.** Escribe `COMPOSE_PROJECT_NAME`, que da a este
  asistente sus propios contenedores y su propio volumen de datos. Sin él, Docker
  deriva el nombre del proyecto de la carpeta y dos clones que se llamen igual
  comparten la base de datos.
- **Re-ejecución.** Con `--force` conserva los puertos que ya tenía este asistente en
  vez de desplazarlos.

Para automatizarlo (CI, scripts, crear varios asistentes):

```bash
agent-core init --non-interactive --name "Mi Bot" --llm deepseek
agent-core init --help    # todas las banderas
```

Qué genera:
- `.env` con claves de cifrado y sesión auto-generadas
- `sdd/*.md` — el System Design Document vivo de TU asistente
- Migraciones aplicadas + usuario admin + configuración base

> ¿La BD no estaba lista? Usa `agent-core init --skip-db` y luego
> `agent-core db migrate && agent-core db seed`.

## 3. Arrancar el asistente

```bash
# Desarrollo (recarga automática):
make dev            # = uvicorn core.main:app --reload --port 8000

# O todo el stack en Docker (API + celery worker + beat):
docker compose up -d --build
```

Verifica: `curl http://localhost:8000/healthz` → `{"status":"ok"}`

> **Importante (desarrollo):** `uvicorn --reload` solo recarga cambios en archivos
> `.py`, **NO** recarga el `.env`. Si editas el `.env` (p. ej. cambias el token de
> WhatsApp o la API key del LLM) mientras el servidor corre, debes reiniciarlo
> manualmente (`Ctrl+C` + `make dev`). Un token viejo en memoria es una causa
> frecuente de "el bot procesa el mensaje pero no responde por el canal".
> Regla práctica: cambiaste el `.env` → reinicia el proceso.

## 4. Entrar al panel

Abre **http://localhost:8000/admin** e ingresa con el email/contraseña del wizard.
Verás el Dashboard con las secciones del core: Conversaciones, Escalaciones, **Conexiones**,
Plugins, Costos LLM y Configuración.

## 5. Conectar WhatsApp (Meta Cloud API)

Hay dos formas (equivalentes):

**A. En el panel (recomendada):** ve a **/admin/conexiones** y completa el formulario
con `label`, `phone_number_id`, `app_secret`, `access_token` y `verify_token`.
Las credenciales se guardan cifradas (Fernet) en `channel_connections` y se **validan
contra la Graph API de Meta antes de guardar** (una credencial mala nunca se persiste).
No requiere reiniciar ni tocar el `.env`.

**B. En el `.env`:**
1. En [developers.facebook.com](https://developers.facebook.com): crea una app tipo
   Business → agrega el producto WhatsApp.
2. Copia al `.env`: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
   `WHATSAPP_APP_SECRET`. El `WHATSAPP_VERIFY_TOKEN` ya lo generó el init.
3. Expón tu servidor local (desarrollo): `cloudflared tunnel --url http://localhost:8000`
4. En Meta → WhatsApp → Configuration → Webhook:
   - Callback URL: `https://<tu-tunel>/webhooks/whatsapp`
   - Verify token: el valor de `WHATSAPP_VERIFY_TOKEN` (el mismo que pusiste en el panel
     si usaste la opción A)
   - Suscríbete al campo `messages`.
5. Reinicia: `docker compose restart core-api` (o el uvicorn local).
6. Escribe al número de prueba de Meta. El mensaje debe aparecer en
   /admin/conversaciones y el asistente debe responder.

> El webhook y el envío resuelven las credenciales en este orden: conexión que matchea
> el `phone_number_id` del payload → conexión primaria activa → `.env`. Mientras no
> guardes conexiones en el panel, todo funciona con el `.env`.
>
> Nota: el token de prueba de Meta caduca cada 24h. Para producción genera un token
> permanente de System User.

## 6. Instalar tu primer plugin

```bash
agent-core plugin install mod-crm-engine
```

Sigue los wizards (datos + exposición de entidades). Al terminar, en el panel:
Plugins → **CRM** → **Configurar** → guarda → el menú lateral agrupa el plugin bajo
"CRM" con sus secciones: Dashboard · Leads · Productos · Categorías · Promociones · Pedidos.

## Solución de problemas

**Empieza siempre por aquí:**

```bash
agent-core doctor
```

Revisa `.env`, Docker, puertos (y quién los ocupa), volumen de datos, conexión a
PostgreSQL y Redis, migraciones del core y de cada plugin, y plugins detectados.
Cada fila que falla dice qué hacer. Sale con código 1 si encuentra algo, así que
sirve en scripts.

### Los tres fallos que más cuestan

**1. `port is already allocated` al levantar**

Otro servicio ocupó el puerto entre el `init` y el `docker compose up`, o lo reclama
un contenedor que estaba parado. `agent-core doctor` dice qué contenedor es. Cambia
`POSTGRES_HOST_PORT` / `REDIS_HOST_PORT` / `APP_HOST_PORT` en el `.env`, o para el
servicio que lo ocupa.

**2. `password authentication failed` o `role "X" does not exist` al migrar**

Casi siempre es el volumen de datos, no las migraciones. PostgreSQL aplica
`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` **solo la primera vez que
inicializa su directorio de datos**. Si el volumen `<proyecto>_pgdata` sobrevive a una
instalación anterior, conserva las credenciales viejas y las del `.env` nuevo se
ignoran, sin ningún aviso que mencione el volumen.

Dos salidas:

```bash
# a) Conservar los datos: pon en el .env las credenciales con las que se creó el volumen
# b) Empezar de cero (DESTRUYE los datos de ese asistente):
docker compose down -v && agent-core init --force
```

`agent-core init --recreate-volume` hace (b) sin preguntar.

**3. Dos asistentes se pisan**

Cada asistente necesita su propio `COMPOSE_PROJECT_NAME` en el `.env`. Sin él Docker
usa el nombre de la carpeta, y dos clones llamados igual comparten contenedores y
volumen. `agent-core doctor` lo marca como aviso.

### Otros síntomas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `docker compose` falla con `.env not found` | No has corrido `init` | `agent-core init` genera el `.env` que compose exige |
| `postgres is unhealthy` y no arranca nada más | El healthcheck se autentica con las credenciales del `.env` | Es el caso 2 de arriba: `docker compose logs postgres` |
| `MASTER_ENCRYPTION_KEY no configurada` | .env sin generar | `agent-core init` o genera la clave manualmente |
| Webhook devuelve 403 en el handshake | verify_token no coincide | Mismo valor en Meta y en `.env` |
| El bot no responde pero guarda mensajes | Kill-switch activo | Panel → Configuración → estado del bot; revisa también cliente y conversación |
| Plugin en estado `error` | Excepción en activación/runtime | El mensaje exacto está en /admin/plugins; corrige y pulsa Activar |
| `alembic upgrade` falla con pgvector | Imagen de Postgres sin la extensión | Usa `pgvector/pgvector:pg16` (ya está en docker-compose.yml) |
| Respuestas nunca llegan | Consumer sin arrancar | El consumer corre dentro de core-api; revisa logs: `docker compose logs core-api` |
