# ADR-0018 — Numeración de pedidos por tenant

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

`crm_orders.order_seq` venía de una secuencia global y `public_order_number` (`OC-####`)
era único global. En multi-tenant, el OC-0002 del tenant A y el OC-0002 del tenant B
colisionan, y `consultar_estado_pedido` (que matchea por número) es ambiguo entre tenants.

## Decisión

La numeración de pedidos pasa a **por tenant**: cada tenant ve `OC-0001` en adelante. Se
implementa con una tabla de contadores `crm_order_counters(tenant_id, last_seq)` y una
función atómica `crm_next_order_seq(tenant)` (`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`).
El `public_order_number` se computa de `order_seq` (Computed existente); el UNIQUE de
`order_seq` y de `public_order_number` pasa a compuesto `(tenant_id, ...)`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Número global con gaps | OC de un tenant "se parece" al de otro; ambigüedad en consultas |
| Secuencia por tenant (N secuencias) | N objetos de BD por tenant; feo de migrar |
| Contador en la app | Carrera entre workers; el lock de conversación no cubre el conteo |

## Consecuencias

**Positivas:** numeración limpia por cliente; `consultar_estado_pedido` aislado por RLS;
el contador atómico (ON CONFLICT) evita colisiones entre workers.

**Negativas / lo que aceptamos pagar:** una tabla + función nuevas en el pack; el
`order_seq` global existente queda como valor interno sin exposición.

**Qué haría que revisáramos esto:** que un cliente exija numeración global continua o
prefijos por sucursal — señal de que la numeración es configuración del pack.

## Impacto en el código

Migración `crm_0002_tenant`, `plugins/pack_commerce/.../models.py`, `skills.py`
(`crear_pedido` asigna el seq por tenant).