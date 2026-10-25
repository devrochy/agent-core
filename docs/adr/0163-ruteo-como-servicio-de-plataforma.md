# ADR-0163 — Ruteo como servicio de plataforma (OSRM propio)

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-35, P-32 (opción A). **Usa:** ADR-0010 (producción en VPS) y el molde de `core/llm/embeddings.py`.

## Contexto

La vertical calcula el tiempo de desplazamiento desde un inmueble hasta un destino del cliente. El servidor demo público de OSRM no admite uso en producción. Las APIs comerciales cobran por consulta, limitan la caché y sacan el destino del cliente a un tercero. Un pack no puede llamar un servicio interno, porque `ScopedHttp` bloquea IP privadas, loopback y CGNAT (`core/mcp/types.py`).

## Decisión

El ruteo es un **servicio del core** que habla con un contenedor OSRM propio con el extracto de Colombia de OpenStreetMap.

1. **Configuración solo de plataforma**: `ROUTING_PROVIDER` (`none|osrm`), `ROUTING_BASE_URL`, `ROUTING_TIMEOUT_SECONDS`, y perfiles carro, bici y a pie.
   - No es configuración del negocio.
   - Con `none`, la función se apaga.
2. **API del core**: `tiempo_de_viaje(origen, destinos, modo)`, que devuelve minutos redondeados a 5. El pack la llama sin HTTP propio.
3. **El servicio no guarda nada.**
   - La caché es del pack, en Redis: `inmo:{tenant_id}:ruta:{inmueble_id}:{celda}:{modo}`, con TTL.
   - Los logs no llevan coordenadas.
4. **Si OSRM no responde**, error de negocio controlado: el pack dice que no está disponible y ofrece las cercanías precalculadas.
5. **Operación**: un contenedor en el VPS. El extracto se actualiza cada mes con un job de plataforma. La RAM del grafo de Colombia se mide antes de producción.
6. **Sin tráfico en tiempo real ni transporte público** (GTFS) en v1.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mapbox o Google Matrix vía `ScopedHttp` | Costo por consulta, licencias que limitan la caché, destino del cliente a un tercero |
| OSRM público | Su política no admite uso en producción |
| URL de OSRM como configuración del negocio | Abre SSRF y choca con la `network_allowlist` estática |

## Consecuencias

**Positivas:** sin costo por consulta, datos que no salen de la plataforma, un servicio que otras verticales pueden usar (rutas de cobro, visitas técnicas).

**Negativas / lo que aceptamos pagar:** operar un contenedor más con su memoria y su actualización mensual, y tiempos sin tráfico.

**Qué haría que revisáramos esto:**
- Que el piloto reporte errores de más del 30 % frente al tiempo real en horas pico.
- Que el contenedor no quepa en el VPS.

## Impacto en el código

- `core/routing/` (nuevo).
- `core/config.py` (settings).
- `docker-compose` y despliegue (contenedor OSRM).
- Pack inmobiliario (caché y llamada).
