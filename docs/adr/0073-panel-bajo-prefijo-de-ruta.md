# ADR-0073 — Panel y URLs de la app bajo un prefijo de ruta del proxy

- **Estado:** aceptado
- **Fecha:** 2026-09-26

## Contexto

agent-core se sirve bajo un **prefijo de ruta** (`/agent-core` en test, `/local`
en local) en el Funnel 443 de Tailscale, porque la raíz del dominio la ocupa
agent-sale y Meta exige HTTPS/443 para el webhook (`docs/despliegue-coolify.md`).
El proxy **quita** el prefijo antes de reenviar (la app recibe rutas limpias),
pero las URLs que la app **genera** son absolutas de raíz: redirects
(`RedirectResponse("/admin/login")`), links de plantillas (`href="/admin/..."`),
estáticos (`/static/...`) y algo de JS (`fetch("/admin/...")`). El navegador las
resuelve contra la raíz del dominio → **agent-sale**, así que el panel no funciona
bajo el prefijo y el callback OAuth de Calendar termina en un 404 de otro servicio.

## Decisión

La app aprende su prefijo con `ROOT_PATH` (vacío = raíz) y un
`PathPrefixMiddleware` reescribe las URLs **salientes**:

- `Location` de cualquier respuesta que empiece con `/` (no `//`).
- URLs absolutas de raíz en respuestas `text/html`: `href`/`src`/`action`,
  `form.action = "..."`, `window.location...`, `fetch("...")` — una sola pasada
  por regex, sin duplicar si ya están prefijadas y sin tocar `//` ni externas.

El middleware **no** toca el routing: el proxy ya quitó el prefijo, la app recibe
rutas limpias, y solo se reescribe la salida. `PUBLIC_BASE_URL` sigue incluyendo
el prefijo (para el webhook de Meta y el `redirect_uri` de OAuth).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Hostname propio en la raíz (Cloudflare Tunnel / VPS) | Fix más limpio pero infra pendiente (`docs/BACKLOG.md`); no bloquea tener esto ya |
| `FastAPI(root_path=...)` + `url_for` | `root_path` no reescribe los literales de plantillas ni `RedirectResponse` |
| Prefijar a mano los 100+ call-sites y plantillas | Enorme y propenso a olvidos; el middleware lo hace en un solo punto |
| Servir la app sin prefijo | La raíz la ocupa otro servicio; Meta exige 443 |

## Consecuencias

**Positivas:** el panel admin y el OAuth funcionan bajo el prefijo (test y local)
sin tocar cada plantilla; un solo punto de configuración (`ROOT_PATH`).

**Negativas / lo que aceptamos pagar:** reescritura de HTML por regex en cada
respuesta (acotada a atributos/llamadas de URL, overhead mínimo); no cubre URLs
generadas dinámicamente sin el prefijo (p. ej. concatenadas en JS complejo).

**Qué haría que revisáramos esto:** un hostname propio en la raíz (→ `ROOT_PATH=""`
y el middleware queda no-op), o URLs de panel generadas en JS que la regex no cubra.

## Impacto en el código

- `core/config.py` (`root_path`/`ROOT_PATH`), `core/admin/path_prefix.py` (nuevo),
  `core/main.py` (montaje del middleware), `.env.example`, `docs/despliegue-coolify.md`.
- Sin cambios en `packages/contracts`.
