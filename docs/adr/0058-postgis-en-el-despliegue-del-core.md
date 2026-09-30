# ADR-0058 — PostGIS en el despliegue del core

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

El core corre Postgres con `pgvector` (memoria semántica). Un pack de cobranza
necesita geografía real (ubicación de clientes, rutas, trayectorias) y
`app-cobranza` ya usa `geography(Point,4326)` con índice GIST. Sin PostGIS en la
imagen, el pack no puede replicar el dominio ni optimizar rutas.

## Decisión

La imagen de Postgres del despliegue pasa a incluir **PostGIS + pgvector** en la
misma base. La extensión se habilita con una migración del pack
(`create extension if not exists postgis`). **No** se levanta una base geo
separada.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Base geo separada | Dos fuentes de verdad, joins imposibles, más infra |
| Guardar lat/lng en columnas numéricas | Pierde índices geoespaciales y funciones `ST_*`; degrada la optimización de rutas |
| Calcular todo en memoria (haversine) | No escala; ya se hacía con limitaciones |

## Consecuencias

**Positivas:** un solo Postgres con vectores y geografía; el pack usa PostGIS
nativo; habilita rutas y optimización futuras.

**Negativas / lo que aceptamos pagar:** el cambio de imagen afecta a **todos** los
tenants → probar en test primero; hay que mantener la imagen (`pgvector+postgis`).

**Qué haría que revisáramos esto:** que la imagen se vuelva frágil de mantener o
que el volumen geo crezca al punto de justificar un servicio dedicado.

## Impacto en el código

- `docker-compose.yml` (imagen de Postgres) y `Dockerfile` si aplica.
- `docs/INSTALLATION.md` y `docs/despliegue-coolify.md`.
- Migración `packages/pack-cobranza/alembic/versions/0002_postgis.py`.
