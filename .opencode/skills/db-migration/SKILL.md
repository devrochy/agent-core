---
name: db-migration
description: >
  Crea o revisa migraciones de base de datos en agent-core. Usar SIEMPRE que se cree una
  tabla, se agregue una columna, se toque supabase/migrations o cualquier archivo .sql.
---

Toda migración cumple, sin excepción:

1. `tenant_id uuid not null` en cualquier tabla de negocio.
2. RLS habilitado con policy de aislamiento:
   ```sql
   alter table <tabla> enable row level security;
   create policy tenant_isolation on <tabla>
     using (tenant_id = current_setting('app.tenant_id')::uuid);
   ```
3. Índice compuesto que empieza por `tenant_id` en toda tabla con volumen.
4. Tabla de pack → prefijo `pack_<nombre>_`. Tabla de core → sin prefijo.
5. Idempotente (`if not exists`) y con `<nombre>.down.sql` que la revierte.
6. Sin borrado físico: `deleted_at timestamptz` + índice único parcial
   (`where deleted_at is null`) donde haya unicidad.
7. `created_at timestamptz not null default now()` en toda tabla.

## Plantilla

```sql
create table if not exists pack_<pack>_<entidad> (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null,
  -- columnas
  custom      jsonb not null default '{}',
  deleted_at  timestamptz,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
alter table pack_<pack>_<entidad> enable row level security;
create policy tenant_isolation on pack_<pack>_<entidad>
  using (tenant_id = current_setting('app.tenant_id')::uuid);
create index if not exists idx_<entidad>_tenant
  on pack_<pack>_<entidad> (tenant_id, created_at desc);
```

## Detente y pregunta si

- La tabla parece un catálogo global compartido entre tenants: probablemente es
  configuración de pack, no una tabla.
- Hay que borrar una columna con datos: eso es una migración en dos fases (deprecar,
  esperar un release, eliminar), nunca en una.

## Antes de terminar

Verifica que existe el `.down.sql` y que aplicar y revertir en local deja el esquema igual.