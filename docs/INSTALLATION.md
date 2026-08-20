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

## 2. Levantar la infraestructura

```bash
docker compose up -d postgres redis
```

Verifica: `docker compose ps` → ambos `healthy`.

## 3. Inicializar el asistente (wizard)

```bash
agent-core init
```

El wizard pregunta: nombre, negocio, idioma, tono, proveedor LLM (DeepSeek por defecto),
API key, embeddings, velocidad de respuesta, URLs de BD/Redis y el admin.

Qué genera:
- `.env` con claves de cifrado y sesión auto-generadas
- `sdd/*.md` — el System Design Document vivo de TU asistente
- Migraciones aplicadas + usuario admin + configuración base

> ¿La BD no estaba lista? Usa `agent-core init --skip-db` y luego
> `agent-core db migrate && agent-core db seed`.

## 4. Arrancar el asistente

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

## 5. Entrar al panel

Abre **http://localhost:8000/admin** e ingresa con el email/contraseña del wizard.
Verás el Dashboard con las secciones del core: Conversaciones, Escalaciones, **Conexiones**,
Plugins, Costos LLM y Configuración.

## 6. Conectar WhatsApp (Meta Cloud API)

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

## 7. Instalar tu primer plugin

```bash
agent-core plugin install mod-crm-engine
```

Sigue los wizards (datos + exposición de entidades). Al terminar, en el panel:
Plugins → **CRM** → **Configurar** → guarda → el menú lateral agrupa el plugin bajo
"CRM" con sus secciones: Dashboard · Leads · Productos · Categorías · Promociones · Pedidos.

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `MASTER_ENCRYPTION_KEY no configurada` | .env sin generar | `agent-core init` o genera la clave manualmente |
| Webhook devuelve 403 en el handshake | verify_token no coincide | Mismo valor en Meta y en `.env` |
| El bot no responde pero guarda mensajes | Kill-switch activo | Panel → Configuración → estado del bot; revisa también cliente y conversación |
| Plugin en estado `error` | Excepción en activación/runtime | El mensaje exacto está en /admin/plugins; corrige y pulsa Activar |
| `alembic upgrade` falla con pgvector | Imagen de Postgres sin la extensión | Usa `pgvector/pgvector:pg16` (ya está en docker-compose.yml) |
| Respuestas nunca llegan | Consumer sin arrancar | El consumer corre dentro de core-api; revisa logs: `docker compose logs core-api` |
