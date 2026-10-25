# ADR-0156 — Vertical inmobiliaria: pack `pack_inmobiliario` con inventario geoespacial

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28 «InmoAgent» de solvershub-docs, HU-INMO-01. **Amplía:** ADR-0154 (geocodificación fuera del perfil, punto 3; 0154 está propuesto y se acepta antes que este). **Usa:** ADR-0164 (supresión por titular), ADR-0165 (salida estructurada del canal), ADR-0001 (RLS), ADR-0004 (tablas por pack), ADR-0023 (nada al core con un solo consumidor), ADR-0053 (PII y purga), ADR-0058 (PostGIS), ADR-0112 a 0119 (Agent OS).

## Contexto

Una inmobiliaria atiende por WhatsApp a compradores, arrendatarios, propietarios, arrendatarios que pagan canon y asesores. agent-core no tiene la entidad inmueble. La búsqueda de commerce es `ILIKE`, sin rangos ni ubicación. PostGIS está instalado pero ninguna consulta de lectura lo usa (cobranza solo mantiene `geom` por trigger). La ubicación que comparte el cliente se pierde en el gateway. El dueño cerró las decisiones D-SPEC-28-4 a 9 y P-27 a P-31 el 2026-10-24.

## Decisión

Se crea un pack de primera parte, `pack_inmobiliario`, con estas reglas. Cada punto nombra su HU.

1. **Inventario geoespacial con aislamiento por negocio** (HU-INMO-10).
   - Tablas `pack_inmobiliario_*` con árbol Alembic propio, `tenant_id` al frente en PK, UNIQUE, EXCLUDE e índices.
   - RLS forzada, únicos parciales `WHERE deleted_at IS NULL` y sin DELETE.
   - La primera migración crea `postgis`, `btree_gist`, `btree_gin`, `pg_trgm` y `unaccent` con `IF NOT EXISTS`, sin depender de la migración de calendar; `unaccent` va envuelta en una función `IMMUTABLE` para el `tsvector`.
   - `inmuebles.geom geography(Point,4326)` calculado por una función de trigger `SECURITY INVOKER` (molde `pack-cobranza/alembic/versions/0002_postgis.py`, sin su índice GIST sin `tenant_id`), con índice GIST `(tenant_id, geom)`.
   - Texto libre con `tsvector` español + `unaccent` + trigramas.
   - Sin vistas con `tenant_id` en `public` (las recorre `purge_tenant`).
2. **Zonas y POI por negocio** (HU-INMO-12, HU-INMO-13, D-SPEC-28-5).
   - `zonas` (multipolígono) y `pois` se importan de OpenStreetMap por ciudad y se guardan en las tablas de cada negocio.
   - Las cercanías se precalculan en `cercanias`.
   - Un catálogo compartido entre negocios requiere un ADR propio.
3. **Geocodificación: pin primero y luego LocationIQ por el servicio del core** (P-27). **Amplía ADR-0154.**
   - Si el cliente o el asesor comparten un pin, se usa el pin.
   - Si no hay pin, el pack llama `core.geocoding.geocodificar` sobre direcciones de **inmuebles** y sobre lugares que nombra quien busca, no solo sobre el perfil del cliente.
   - La cuota sigue siendo `consumir_cuota(tenant_id)`. La importación masiva del panel tiene su propio tope, `GEOCODING_MAX_IMPORTACION_POR_DIA`, y se procesa por lotes en una tarea `register_task`, para no gastar en un día el tope de 200 del perfil.
   - La importación cuenta también contra el tope global, y su tope propio no puede pasar del 25 % del global: una inmobiliaria no deja sin geocodificación al resto de los negocios.
   - Para confirmar se necesitan hasta 3 candidatos: el core agrega una función que los devuelve (hoy `geocodificar` pide `limit: 1`) y deja `geocodificar` intacta para el perfil. El modelo nunca elige.
   - El resultado va a `geocache` del negocio: consulta como HMAC con una llave derivada por negocio de un secreto de plataforma, `expira_at` según la licencia del proveedor, nunca compartida.
   - Las coordenadas del inmueble viven en `inmuebles`, no en hechos `perfil`.
4. **Búsqueda, scoring y relajación en el servidor** (HU-INMO-14, HU-INMO-15).
   - El LLM solo llena parámetros tipados.
   - El servidor filtra, mide con `ST_DWithin` y `ST_Distance`, puntúa con pesos por negocio, explica y propone relajaciones con su conteo.
   - Precios, distancias, direcciones, horarios y links nunca salen del modelo.
5. **Privacidad de la dirección** (D-SPEC-28-8, P-30).
   - Mientras el cliente no tenga una visita confirmada a ese inmueble, se muestran barrio y distancias redondeadas a 100 m.
   - Con la visita confirmada se da la dirección exacta.
   - Es configurable por negocio: `revelar_direccion` ∈ `nunca|tras_agendar|siempre`.
   - Los cálculos de desplazamiento sin visita parten de la celda gruesa, no del punto exacto.
6. **Entrada y salida estructurada del canal** (HU-INMO-02, HU-INMO-03, D-SPEC-28-9): la decide ADR-0165, porque es un cambio del core que también usaría commerce. La vertical usa índices opacos (`inm:3`) en las filas y valida el payload contra lo que el servidor guardó de la conversación. El botón es de HU-AOS-09 de punta a punta.
7. **Pipeline comercial propio** (HU-INMO-20, D-SPEC-28-7).
   - `oportunidades` y `oportunidad_eventos` (append-only) en el pack.
   - El lead es el `Customer` del core (ADR-0022) y se muestra en la vista global de leads por `LeadProvider` (ADR-0056).
8. **Especialistas** (HU-INMO-30, D-SPEC-28-6).
   - Con el Agent OS: Coordinador + `asesor_comercial`, `captacion` y `arriendos` + General.
   - Geo, match y relevo son cálculos o back-office, no agentes.
   - Antes del Agent OS: secciones de `flow.json`.
9. **Supresión por titular** (HU-INMO-09): el punto de extensión del core lo decide ADR-0164. El pack lo implementa:
   - anonimiza ubicación, preferencias implícitas, datos de captación, documento, parámetros del CMA, destino de desplazamiento y oferta;
   - borra las claves `inmo:{tenant_id}:{conversation_id}:*` de cada conversación del titular;
   - las tablas append-only lo permiten por trigger, nunca por RULE.
10. **Archivos** con `core.almacenamiento`, bajo `TENANTS_ROOT/<slug>/{inmuebles,captaciones,informes}/`: los borra la purga del negocio.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Extender `pack_commerce` con el inmueble | Su catálogo es de productos con stock. Mezclaría ciclos de vida y obligaría a activar commerce |
| Embeddings para la búsqueda en v1 | Los filtros y el geo cubren lo medible. La semántica entra apagada y medida (adenda de ADR-0145, HU-INMO-40) |
| Mapbox o Google como geocodificador | Licencias que limitan guardar el resultado. LocationIQ ya está integrado (ADR-0154) |
| Siete agentes, uno por "subagente" del encargo | Multiplica los errores de ruta. Geo, match y relevo no conversan (D-27) |

## Consecuencias

**Positivas:** una vertical sin fork que reutiliza agenda, suscripciones, pagos, conocimiento y verificador. Las cifras y la dirección quedan bajo control del servidor.

**Negativas / lo que aceptamos pagar:**
- Un pack grande (unas 15 tablas).
- La cobertura de OpenStreetMap en direcciones colombianas es irregular, y se compensa con pin y corrección en el panel.
- La importación geocodifica por lotes y puede tardar más de un día con inventarios grandes.

**Qué haría que revisáramos esto:**
- Que más del 10 % de las ubicaciones del piloto necesiten corrección del asesor.
- Que una segunda vertical necesite inventario geoespacial: en ese caso, la parte genérica se sube al core con su ADR.

## Impacto en el código

- `packages/pack-inmobiliario/` (nuevo).
- `core/geocoding/service.py`: función que devuelve hasta 3 candidatos; `geocodificar` queda igual para el perfil.
- `core/geocoding/config.py`: `GEOCODING_MAX_IMPORTACION_POR_DIA`, con tope del 25 % del global.
- Secreto de plataforma para el HMAC de `geocache`.
- Lo del canal y la supresión, en ADR-0165 y ADR-0164.
