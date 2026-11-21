# Despliegue de la API sin corte: Traefik de Coolify y health check

> **Estado: propuesta, sin ejecutar (2026-11-20).** Solo describe pasos; nada de esto se aplicó
> en test ni en producción. Lo ejecuta el dueño, primero en test y con su OK en producción.
> Revisa la decisión de [`produccion-vps.md`](produccion-vps.md) (paso 10 de la fase de Coolify:
> «el proxy de Coolify (Traefik) no se usa») y la topología de
> [`despliegue-coolify.md`](../despliegue-coolify.md).

## Problema

`agent-core-api` publica `127.0.0.1:3003:8000` y el túnel de Cloudflare apunta a ese puerto. Con
un puerto fijo en el host, Coolify **no puede** hacer rolling update: el contenedor nuevo no puede
ligarse al mismo puerto mientras el viejo sigue vivo, así que Coolify detiene el viejo y después
arranca el nuevo (documentación de Coolify, *Rolling updates → Settings that prevent a rolling
update*: «A port is published on the host»). El corte dura todo el arranque del nuevo. Mientras
tanto, cloudflared devuelve 502 a Meta, Twilio, Wompi, Telegram, el chat web y el panel.

Medición del arranque (logs del contenedor, con marca de tiempo):

| Etapa | Prod 2026-11-20 03:40 UTC (antes) | Con la imagen que hornea los packs |
| --- | --- | --- |
| `pip install -e` de 7 packs | 29,0 s | 0 s (ver el PR de este documento) |
| migraciones (core + packs) | 7,2 s | igual |
| `sync-packs` | 4,6 s | igual |
| importar `core.main` hasta `Started server process` | 6,7 s | igual |
| startup de la app | 0,4 s | igual |
| **total hasta `Uvicorn running`** | **~48 s** | **~19 s** |

Hornear los packs baja el corte a unos 19 s, pero no lo elimina. El 2026-11-19 a las 22:40
(Bogotá) un webhook de Twilio se perdió así: sus reintentos caben en segundos. Para quitar el
corte hace falta que el contenedor viejo siga atendiendo hasta que el nuevo esté sano.

## Propuesta

```
cloudflared (systemd) ──► 127.0.0.1:80  Traefik (coolify-proxy)
                                  │  Host: api.solvershub.site
                                  ├─► agent-core-api (contenedor viejo, healthy)
                                  └─► agent-core-api (contenedor nuevo, cuando pase /healthz)
```

1. `agent-core-api` **sin Ports Mappings**, con dominio (FQDN) en Coolify y health check HTTP
   `/healthz` activo. Así Coolify genera las etiquetas de Traefik y puede tener los dos
   contenedores a la vez.
2. Coolify arranca el contenedor nuevo junto al viejo y espera su health check. Cuando pasa,
   retira el viejo. Si falla, borra el nuevo y deja el viejo sirviendo (*Health checks and rolling
   updates* en la documentación de Coolify).
3. El túnel deja de apuntar a `localhost:3003` y apunta a Traefik, en `localhost:80`, con el
   `Host` de la API.

### Condiciones que Coolify exige para el rolling update

Según la documentación de Coolify. Verificar contra la versión instalada: el Coolify de test es
4.7.6 y la de producción la confirma el dueño.

- Ningún puerto publicado en el host (Ports Mappings vacío).
- *Container naming* distinto de «Consistent name (no rolling updates)» y sin *Custom Container
  Name*. Un *Container Name Prefix* no estorba.
- Sin `--ip`/`--ip6` en Custom Docker Options.
- Health check configurado. Sin él, Coolify no sabe cuándo el nuevo está listo.
- El log del deploy dice explícitamente si el rolling update no aplica y por qué: es lo primero
  que hay que leer en la prueba de test.

## Riesgos y cómo se cubren

| Riesgo | Qué pasa | Cómo se cubre |
| --- | --- | --- |
| **Firma de Twilio** | Twilio firma la URL pública completa. Si la app la reconstruyera a partir de la petición, un proxy en medio podría cambiar el `https` o el host y dar 403 a todos los webhooks. | No aplica hoy: la firma se valida contra la URL **registrada** en la conexión (`core/gateway/twilio/webhook.py:200-209`, `webhook_inbound_url`/`webhook_status_url`), no contra `request.url`. Meta y Wompi firman el cuerpo. Igual se prueba en test (paso 6). |
| **IP del cliente** | Con Traefik en medio, la app ve la IP del contenedor de Traefik y no la de cloudflared. | `TRUSTED_PROXY_CIDRS` por defecto incluye `172.16.0.0/12` y `10.0.0.0/8` (`core/config.py:111-118`), las redes de Docker. El bloqueo de login sigue leyendo `CF-Connecting-IP`. |
| **Alias de red `agent-core-api`** | El panel y la landing llaman a `http://agent-core-api:8000` por la red `coolify`. Durante el solape los dos contenedores pueden tener el alias. El DNS de Docker reparte entre ambos sin mirar la salud, así que el panel puede pegarle al nuevo antes de que escuche (conexión rechazada unos segundos). | Comprobar en test si Coolify pone el alias en el contenedor nuevo antes de que pase el health check (`docker inspect … NetworkSettings.Networks.coolify.Aliases`). Si lo pone, el panel y la landing pasan a llamar por Traefik (`AGENT_CORE_API_URL=http://coolify-proxy` con el `Host` de la API) o el BFF reintenta una vez ante conexión rechazada. Decisión del dueño. |
| **Migraciones en el solape** | El contenedor nuevo migra mientras el viejo sigue sirviendo con el código anterior. | Las migraciones tienen que ser compatibles hacia atrás durante esos segundos: expandir primero y contraer en una release posterior (skill `db-migration`). Una migración que rompa el código viejo se despliega en una ventana con el corte aceptado. |
| **Worker y beat** | No tienen HTTP ni puertos. Dos beats a la vez duplican los crons. | El rolling update es **solo para la API**. El beat queda como hoy (detener y arrancar) y lo mismo el worker, salvo una decisión aparte: dos workers a la vez son seguros con el claim de salientes, pero eso no entra aquí. |
| **Traefik expuesto** | Traefik escucha en `0.0.0.0:80/443` por defecto. | En prod-app el firewall del proveedor ya cierra todo salvo SSH ([Gotchas](../despliegue-coolify.md#gotchas-conocidos)): verificar con `nc -zv -w 5 <ip-publica-app> 80` desde fuera **antes** de enrutar el túnel. No exponer el dashboard de Traefik. |
| **Dos hostnames hacia la API** | Entran a la API `api.solvershub.site` y `app.solvershub.site/webhooks/` (las URL registradas en Meta y Wompi). | Dar los dos dominios al recurso en Coolify o, más limpio, que la ruta de `app.…/webhooks/` en el túnel fije `httpHostHeader: api.solvershub.site` (*Additional application settings → HTTP Settings → HTTP Host Header*). Así Traefik solo enruta un `Host`. |
| **Traefik caído** | Si el contenedor `coolify-proxy` cae, cae la API entera, y antes no dependía de él. | Coolify lo reinicia solo y el health check del túnel lo muestra. El panel y la landing pueden seguir por puerto fijo, porque su corte no pierde webhooks. Solo la API pasa por Traefik. |

## Pasos (los ejecuta el dueño)

### En test (Coolify local, API hoy en `0.0.0.0:3013`)

1. Respaldar la configuración actual de la API de test: captura de *General*, *Network* y
   *Health check*, y la ruta del túnel de test hacia `:3013`.
2. Coolify → la API de test → *General* → Domains: `https://test.solvershub.site` (o el hostname
   que use el túnel de test). Ports Mappings: **vacío**. Ports Exposes: `8000`.
3. *Health check*: activado, HTTP `GET /healthz`, puerto `8000`, intervalo 5 s, start period
   60 s, reintentos 3.
4. *Advanced*: container naming distinto de «Consistent name», sin Custom Container Name.
5. Zero Trust → túnel de test → la ruta de la API: servicio `http://localhost:80` y *HTTP Host
   Header* = el dominio del paso 2. Las rutas del panel de test no se tocan.
6. Redeploy de la API y, durante el deploy, en otra terminal:

   ```bash
   while true; do date +%T; curl -s -o /dev/null -w '%{http_code}\n' https://test.solvershub.site/healthz; sleep 0.5; done
   ```

   Esperado: solo 200 durante todo el deploy. En el log de Coolify debe verse que arranca el
   contenedor nuevo junto al viejo, que espera el health check y que retira el viejo. Si dice
   «rolling update not supported», el log nombra la condición que falta.
7. Probar los webhooks reales por la ruta nueva: un mensaje entrante de Twilio, uno de Telegram y
   el chat web. Ninguno debe dar `twilio_webhook_signature_invalid` ni 403.
8. Repetir el paso 6 con un deploy cuyo contenedor nuevo falle el health check (por ejemplo, con
   una variable rota a propósito): el viejo debe seguir sirviendo.
9. Revisar el alias de red durante el solape (riesgo «Alias de red») y anotar el resultado en
   este documento.

### En producción (solo con OK del dueño tras test)

Los mismos pasos 1–6 sobre `agent-core-api` de prod-app, con el túnel `solvershub-prod`:

- Ruta `api.solvershub.site` (`^/webhooks/` y `^/healthz$`) → `http://localhost:80`.
- Ruta `app.solvershub.site` (`^/webhooks/` y `^/healthz$`) → `http://localhost:80` con *HTTP
  Host Header* `api.solvershub.site`.
- El panel (`:3004`) y la landing (`:3005`) siguen como están.

Hacerlo en una hora de poco tráfico. Para el instante del cambio de ruta en el túnel, la
reconciliación de Twilio (rama `fix/twilio-entrantes-sin-perdida`) recupera lo que no llegue.

### Vuelta atrás

1. Túnel: las rutas de la API vuelven a `http://localhost:3003` (test: `:3013`) sin *HTTP Host
   Header*.
2. Coolify: Ports Mappings `127.0.0.1:3003:8000` y quitar el dominio. Redeploy: este deploy sí
   tendrá el corte de siempre (~19 s con la imagen que hornea los packs).

## Fuera de alcance

- Hacer más rápido el import de la app (~6,7 s) y `sync-packs` (~4,6 s): con rolling update dejan
  de importar para el corte.
- Rolling update del worker, el beat, el panel y la landing.
