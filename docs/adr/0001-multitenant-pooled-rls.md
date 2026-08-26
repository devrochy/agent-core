# ADR-0001 — Multi-tenant pooled con RLS, sin forks por cliente

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

Los agentes son muy personalizados por cliente, lo que empuja intuitivamente hacia una
aplicación por cliente. Con el objetivo de 200–300 clientes y un solo desarrollador, el
costo de mantenimiento de N despliegues divergentes crece lineal con N: un cambio en la
API de Meta se convierte en N pull requests, N despliegues y N regresiones posibles.

## Decisión

Un único código base multi-tenant. La personalización se resuelve en cuatro niveles, en
este orden de preferencia:

1. **Configuración** — persona, tono, umbrales, catálogo, plantillas. Filas en base de datos
   validadas contra JSON Schema.
2. **Composición** — qué packs, tools y canales tiene activos el tenant.
3. **Extensión** — plugin propio del tenant, versionado y con contrato, cuando ningún pack cubre el caso.
4. **Aislamiento** — topología de despliegue: `pooled` (por defecto), `silo` (datos separados),
   `dedicado` (stack completo aparte).

**Mismo binario, distinta topología.** El día que un tenant requiera una imagen distinta,
se ha perdido el framework.

Aislamiento de datos: `tenant_id` en toda tabla de negocio + RLS en Postgres como última
línea de defensa contra un `where` olvidado.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| App por cliente | Costo O(N); a los 20 clientes ya es inviable para una persona |
| Base de datos por tenant desde el día 1 | Complejidad de migraciones × N sin beneficio hasta que un cliente lo exija |
| Multi-tenant solo a nivel de aplicación (sin RLS) | Un solo bug de query filtra datos entre clientes; riesgo inaceptable en una venta B2B |

## Consecuencias

**Positivas:** costo marginal por cliente cercano a cero; un parche llega a todos a la vez;
posible dar de alta un cliente sin escribir código.

**Negativas:** disciplina permanente (`tenant_id` en todo, RLS en todo); un bug de
aislamiento afecta a todos, no a uno; requiere fairness de colas para que un tenant ruidoso
no ahogue a los demás.

**Qué haría que revisáramos esto:** un cliente con requisito regulatorio de que sus datos no
compartan instancia. Se resuelve con modo `silo` o `dedicado`, no con un fork.

## Impacto en el código

Todas las migraciones, `ToolContext.db`, el JWT de Supabase (claim `tenant_id`), y el
gateway.
