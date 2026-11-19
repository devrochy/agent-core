# Despliegue de planes e inscripción en suscripciones (SPEC-43, ADR-0177)

Cómo desplegar la migración `pack_subscriptions_0010` (tabla de planes, inscripción con `pendiente_pago`,
número público) y qué verificar. **Solo para el entorno de test, con el OK del dueño y el token en la
sesión. Producción nunca desde este plan.**

## ⚠ La migración `0010` BORRA datos

`pack_subscriptions_0010` borra **todas las filas de `pack_subscriptions_subscriptions` y de
`pack_subscriptions_charges`** (de todos los negocios) porque hoy son datos de test descartables
(D-55). Lo hace con la RLS deshabilitada solo durante el borrado y la vuelve a habilitar y forzar al final
(la migración verifica que no queden filas y que las cuatro tablas del pack terminen con RLS habilitada, forzada y con su policy). El `downgrade` revierte el esquema, **no restituye las filas**.

- **Nunca** la corras contra una base que no sea de test o la efímera local.
- Antes de desplegar en cualquier base, haz el precheck de conteo de abajo y compáralo con lo que el dueño
  espera perder. Si hay suscripciones o cobros que NO son de prueba, **detente**: no se despliega.
- Orden de despliegue en test: backend (migraciones) → `agent-core tenant seed <slug>` del negocio que vende
  planes (lee `planes.json`) → panel → habilitar `pack_subscriptions` en el AgentSpec del negocio → smoke.

## 1. Precheck (solo lectura)

```sql
-- Qué se va a borrar, por negocio
select t.slug, count(distinct s.id) as suscripciones, count(c.id) as cobros,
       count(*) filter (where c.estado = 'pagado') as cobros_pagados
from pack_subscriptions_subscriptions s
join tenants t on t.id = s.tenant_id
left join pack_subscriptions_charges c on c.subscription_id = s.id
group by t.slug order by 2 desc;

-- Total (debe coincidir con lo que el dueño aceptó perder)
select (select count(*) from pack_subscriptions_subscriptions) as suscripciones,
       (select count(*) from pack_subscriptions_charges) as cobros;
```

Si `cobros_pagados` es mayor que cero en un negocio real, es plata cobrada de verdad: **no se despliega**.
Respaldo antes de tocar la base: `pg_dump` de las dos tablas.

## 2. Aplicar

El entrypoint corre `agent-core db migrate` (cabeza de `pack_subscriptions`: `pack_subscriptions_0010`). Para
la base efímera local:

```bash
cd packages/pack-subscriptions && python -m alembic upgrade head   # el alembic.ini del pack usa rutas relativas
```

## 3. Verificar

```sql
select version_num from alembic_version_pack_subscriptions;                 -- pack_subscriptions_0010
select relname, relrowsecurity, relforcerowsecurity from pg_class
 where relname like 'pack_subscriptions_%' and relkind = 'r';                 -- t | t en todas
select count(*) from pack_subscriptions_subscriptions;                       -- 0 tras la migración
```

Siembra de los planes (idempotente, todo o nada): `agent-core tenant seed <slug>` lee
`TENANTS_ROOT/<slug>/planes.json`. El precio de un plan con `plan_plataforma` sigue a `core/billing/plans.py`
(cron `sincronizar_precios_de_plan`, cada hora).

## 4. Revertir

`cd packages/pack-subscriptions && python -m alembic downgrade pack_subscriptions_0009` quita las
columnas, la tabla de planes y el contador. Las filas borradas no vuelven: restáuralas del `pg_dump` si
las necesitas.

## 5. Después del despliegue

- Una inscripción `pendiente_pago` expira a los 7 días (`dias_pago_inscripcion`) con el cron `expirar_inscripciones`.
- Un pago que llega tarde o a un cargo anulado no activa nada: queda `subs_pago_sin_aplicar` en la auditoría y
  una notificación a los dueños para resolverlo a mano.
- `plan_id` y `numero_publico` son NOT NULL desde `0013` (SPEC-43 PR-4, commerce sin planes): la migración retira
  `variante_id` y `referencia_origen`, y se detiene (sin cambiar nada) si alguna suscripción no tiene plan o número:
  hay que asignárselos o borrarlos antes de aplicarla.
