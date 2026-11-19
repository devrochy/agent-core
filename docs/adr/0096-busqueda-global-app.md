# ADR-0096 — Búsqueda global de la app oficial con providers registrados por los packs

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Contexto:** HU-APP-14 (SPEC-25, solvershub-docs)

## Contexto

La app oficial (SPEC-25) necesita una búsqueda global: un solo endpoint que encuentre clientes,
conversaciones, productos, pedidos, citas y promesas respetando los permisos por sección del RBAC
(ADR-0047) y el aislamiento por negocio (RLS). El core no puede importar de los packs (invariante
5), así que no puede consultar sus tablas directamente; y la app no puede consultar una docena de
endpoints con filtros propios por módulo (sería inconsistente y lento).

## Decisión

**Un endpoint del core (`GET /api/v1/search`) + un registro de providers**: los packs registran
providers de búsqueda al cargarse; el core agrega resultados sin conocer sus tablas.

- `core/search/__init__.py`: `register_search(section, label, provider)` con firma
  `provider(tenant_id, q, limit) -> list[Resultado]`. `Resultado` uniforme:
  `{id, tipo, titulo, subtitulo, extra}` para que la app arme deep links por (sección, tipo).
- `core/api/routers/search.py`: valida `q` (2–100 caracteres) y `limit` (1–20), resuelve el
  principal, y por cada sección registrada: (a) exige acceso de lectura vía
  `effective_access(admin, section, tenant_id)` — los packs declaran su sección RBAC
  (`pack_cobranza:clientes`, `pack_commerce`, `pack_calendar:citas`, …), master bypass — y (b)
  llama al provider dentro del scope de tenant del request (RLS aplica, fail-safe). Un provider
  que falla se loguea y omite su sección, nunca tumba la búsqueda.
- Providers registrados en v1: `conversations` y `leads` (core, sobre `Customer`/`Conversation`),
  `pack_cobranza:clientes`, `pack_cobranza:promesas`, `pack_commerce` (productos + pedidos) y
  `pack_calendar:citas`. El matching es `ILIKE` con wildcards escapados y límite por sección.
- Los providers se ejecutan en paralelo (`asyncio.gather`); la respuesta agrupa por sección con su
  label para la UI móvil.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| El core consulta tablas de packs | Rompe la invariante 5 (el core no depende de packs) |
| Endpoints de búsqueda por pack y agregación en la app | N llamadas, permisos resueltos en el cliente y UX inconsistente |
| Motor de búsqueda externo (tiposense/meilisearch) | Infraestructura nueva para un volumen pequeño; el ILIKE con índices alcanza en Fase 1; migrar después es barato |

## Consecuencias

**Positivas:** una sola superficie con RBAC centralizado y RLS; los packs se agregan sin tocar el
core; fallo aislado por sección; contrato de resultado uniforme para deep links.

**Negativas / lo que aceptamos pagar:** el matching es ILIKE simple (sin ranking por relevancia ni
fuzzy); si una sección crece, habrá que indexar (`pg_trgm`) o migrar a un motor dedicado.

**Qué haría que revisáramos esto:** búsqueda con ranking/typos, volumen que sature Postgres, o
necesidad de buscar dentro del contenido de mensajes.

## Impacto en el código

- `core/search/{__init__,providers}.py`, `core/api/routers/search.py`, `core/api/__init__.py`.
- `packages/pack-cobranza/pack_cobranza/search.py` (importado por `admin_api.py`),
  `packages/pack-commerce/pack_commerce/search.py`, `packages/pack-calendar/pack_calendar/search.py`.
- `docs/openapi.json` regenerado. Sin migración (sin tablas nuevas).
