# ADR-0002 — Gateway propio como punto de ingreso; Chatwoot desacoplado

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

Una topología común es poner Chatwoot como receptor de los webhooks de Meta y que el sistema
de agentes actúe como cliente suyo. Eso convierte a Chatwoot en punto único de falla,
acopla el modelo de datos a su esquema y añade dos saltos de red a cada turno de una
conversación de WhatsApp, donde la latencia es visible para el usuario final.

## Decisión

El webhook de Meta entra directamente al gateway de agent-core. Nuestra base de datos es la
fuente de verdad. Chatwoot se alimenta de forma asíncrona como consola de atención humana,
es opcional por tenant y se ofrece como add-on a clientes con equipo humano de 3 o más personas.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Chatwoot como ingreso | SPOF, latencia extra, acoplamiento de esquema, trazabilidad partida |
| Sin bandeja humana | El handoff a humano es requisito de venta en casi todos los nichos |

## Consecuencias

**Positivas:** latencia mínima; trazabilidad y atribución de costos completas en un solo
sitio; Chatwoot es reemplazable.

**Negativas:** mantenemos un sync propio y una bandeja mínima; duplicación parcial de datos
de conversación.

**Qué haría que revisáramos esto:** que la bandeja propia se convierta en un producto que
consuma más mantenimiento del que ahorra.

## Impacto en el código

`apps/gateway`, `packages/channel-*`, servicio de sincronización con Chatwoot.
