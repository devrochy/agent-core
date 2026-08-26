---
name: agentcore-conventions
description: >
  Convenciones y límites de capa del framework agent-core. Usar siempre que se escriba o
  modifique código en packages/ o apps/, cuando haya duda sobre dónde va un archivo, o
  cuando se decida si algo es core, pack, configuración o plugin del tenant.
---

## Dónde va cada cosa

| Va al… | Si… |
|---|---|
| **core** | lo necesitan >=2 verticales, no menciona el dominio del cliente, su ausencia rompe el runtime |
| **pack** | nombra una entidad de nicho, su tabla lleva prefijo natural, un tenant podría no tenerlo |
| **configuración** | es un número, texto, umbral o booleano que el cliente cambiaría solo |
| **plugin del tenant** | solo un cliente lo pide y no es generalizable |

Ante la duda: configuración primero, pack después, core al final. Subir algo al core es
difícil de revertir.

## Límites de capa

- `packages/contracts` no importa de nadie.
- `packages/pack-*` importa de core, nunca de otro pack.
- `apps/*` no contiene lógica de dominio; orquesta paquetes.
- Los adaptadores de canal normalizan a `InboundMessage` y no conocen el dominio.

## Nombres

- Tablas de core: `contact`, `conversation`, `deal` (singular, sin prefijo).
- Tablas de pack: `pack_<pack>_<entidad>`.
- Tools: `<dominio>.<verbo>_<objeto>` → `inventory.check_stock`, `booking.book_slot`.
- Scopes: `<recurso>:<sub>:<acción>` → `crm:contacts:read`.
- Eventos de dominio: `<entidad>.<pasado>` → `appointment.created`.

## Prohibiciones

- `any` y `unknown` sin narrowing en código de producción.
- Acceso a base de datos fuera de `ctx.db` dentro de un pack o tool.
- `process.env` dentro de `packages/pack-*`.
- Prompts de sistema en archivos `.ts`.
- Tablas de negocio sin `tenant_id`.