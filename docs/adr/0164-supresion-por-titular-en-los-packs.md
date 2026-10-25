# ADR-0164 — Supresión de datos por titular en los packs

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-09. **Enmienda:** ADR-0053 (la purga por cliente llega a los packs). **Usa:** ADR-0161.

## Contexto

La Ley 1581 da al titular el derecho a suprimir sus datos. Hoy la purga por cliente solo toca la memoria (`purge_memory`, `core/memory/vector.py:223`). Los packs guardan datos personales en sus tablas: commerce, cobranza, calendar y, con la vertical, ubicación, captaciones, ofertas y destinos. No existe un punto de extensión para que cada pack suprima lo suyo. `purge_tenant` borra el negocio entero, no a un titular.

## Decisión

El core define un punto de extensión opcional por pack para suprimir los datos de un titular.

1. **Método opcional** `BaseModule.suprimir_titular(ctx, customer_id) -> ResumenSupresion` (`core/mcp/types.py`).
   - Por defecto no hace nada.
   - Cada pack anonimiza o borra lo suyo y devuelve qué tocó (tablas y conteos, nunca datos).
2. **Quién lo invoca:** una acción del core «Suprimir datos del titular», solo propietario, con confirmación.
   - Corre dentro de `tenant_scope` del negocio.
   - Llama a `purge_memory` y al hook de cada pack habilitado, cada uno en su propia transacción.
   - Un pack que falla no deshace a los demás: queda registrado y se puede reintentar (el hook es idempotente).
3. **Disparadores:** la acción del panel y la revocación de `tratamiento_datos` (ADR-0161).
4. **Registro:** `audit_log` guarda quién, cuándo, qué packs y sus resúmenes. Nunca los datos suprimidos.
5. **Tablas append-only:** la anonimización la permite un trigger que solo admite poner en nulo o en marcador las columnas personales. Nunca por RULE (rompería `purge_tenant`).
6. **Redis:** cada pack borra sus claves por conversación del titular. Las claves llevan `tenant_id` en primera posición para que también las alcance la purga del negocio.
7. **Lo que la ley obliga a conservar** (por ejemplo, contratos vigentes) se conserva y se informa en el resumen. Se valida con el abogado (HU-GTM-02).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Que el core borre las filas de los packs por `customer_id` | Viola la invariante 5 (el core no conoce el esquema de los packs) y rompería tablas append-only |
| Supresión solo de la memoria, como hoy | Deja datos personales en las tablas de los packs |

## Consecuencias

**Positivas:** una sola acción para atender el derecho de supresión, que alcanza a todos los packs presentes y futuros.

**Negativas / lo que aceptamos pagar:** cada pack tiene que implementar y probar su hook. Hasta que lo hagan, la acción informa qué packs no lo tienen.

**Qué haría que revisáramos esto:** que el abogado pida supresión asíncrona con plazo legal y notificación al titular, lo que pediría una cola y un estado de solicitud.

## Impacto en el código

- `core/mcp/types.py` (`BaseModule.suprimir_titular`).
- Acción del core y ruta de la API (con `docs/openapi.json`).
- `core/memory/vector.py` (se integra a la acción).
- Hooks en `pack_inmobiliario` y, con el tiempo, commerce, cobranza y calendar.
