---
name: db-migration
description: >
  Crea o revisa migraciones Alembic de agent-core (core y packs). Usar SIEMPRE que se cree
  una tabla, se agregue o cambie una columna, o se toque `alembic/versions/` o
  `packages/pack-*/alembic/versions/`.
---

El esquema se versiona con **Alembic en Python** (no hay `.sql` sueltos ni Supabase).
`agent-core db migrate` (`make migrate`, `core/db/migrations.py::upgrade_all`) aplica
primero el árbol del core hasta su head y después el árbol propio de cada pack.

## Dónde va

| Qué | Archivo | Revisión |
|---|---|---|
| Tabla del core | `alembic/versions/00NN_<slug>.py` | `revision = "00NN"`, `down_revision = "<head anterior>"` |
| Tabla de pack | `packages/pack-<x>/alembic/versions/NNNN_<slug>.py` | `revision = "pack_<x>_NNNN"`, `down_revision = "pack_<x>_<NNNN-1>"` (`None` en la primera) |

- El head del core se ve con `uv run alembic heads` (tiene que haber **uno**). Los números
  no son contiguos (no existe `0039`): el siguiente es el head + 1.
- Cada pack usa su propia tabla de versiones `alembic_version_pack_<x>` (la fija
  `upgrade_plugin` y la repite el `alembic.ini` del pack).
- Tablas de pack con prefijo `pack_<x>_`; del core, sin prefijo.
- Por el orden de ejecución, una migración del core **no puede** depender de tablas de un
  pack. Un pack referencia al core o a otro pack solo con un UUID lógico, sin FK
  (como `customer_id` y `variante_id` en `pack-subscriptions/0001`): ningún pack importa a otro.
- El modelo SQLAlchemy se actualiza en el mismo cambio (`core/db/models.py` o
  `pack_<x>/models.py`).

## Toda tabla de negocio cumple, sin excepción

1. `tenant_id uuid not null`.
2. RLS **habilitada y forzada**, con la policy de aislamiento usando `nullif` en `USING`
   y en `WITH CHECK`:
   ```sql
   alter table <t> enable row level security;
   alter table <t> force row level security;
   create policy tenant_isolation on <t>
     using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
     with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
   ```
   - Sin `nullif`, `current_setting` devuelve `''` fuera de un `tenant_scope` y `''::uuid`
     revienta. Con `nullif`, la consulta falla cerrada: devuelve 0 filas.
   - Sin `FORCE`, el dueño de la tabla (el rol de migraciones) se salta la policy. Además,
     el checklist de producción (`rls_forced`, `core/gateway/meta_production_readiness.py`)
     queda en rojo.
   - `0029` y los `*_force_row_level_security.py` de cada pack solo forzaron las tablas que
     existían en ese momento: **cada tabla nueva trae su propio `FORCE`**.
3. Índice compuesto que empieza por `tenant_id` (el orden de las columnas siguientes sale
   de cómo se consulta).
4. Sin borrado físico: `deleted_at timestamptz` + índice único parcial
   (`where deleted_at is null`) donde haya unicidad.
5. `created_at` / `updated_at timestamptz not null default now()`.
6. `grant select, insert, update, delete on <t> to app_runtime` explícito. Los default
   privileges de `0003` solo valen si la tabla la crea el mismo rol que corrió `0003`.
7. Montos con `Numeric(14, 2)`, nunca `float`. Estados como texto con valores
   documentados en el modelo.
8. `downgrade()` que revierte en orden inverso: policy → `no force` → `disable` → índices
   → tabla.

## Plantilla (pack)

```python
"""<Qué agrega y por qué> (ADR-00NN si aplica).

Revision ID: pack_<x>_NNNN
Revises: pack_<x>_<NNNN-1>
Create Date: AAAA-MM-DD
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_<x>_NNNN"
down_revision = "pack_<x>_<NNNN-1>"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)
_TABLE = "pack_<x>_<entidad>"


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        # columnas de negocio
        sa.Column("custom", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(f"ix_{_TABLE}_tenant", _TABLE, ["tenant_id", "created_at"])
    _rls(_TABLE)


def downgrade() -> None:
    op.execute(f"drop policy if exists tenant_isolation on {_TABLE}")
    op.execute(f"alter table {_TABLE} no force row level security")
    op.execute(f"alter table {_TABLE} disable row level security")
    op.drop_index(f"ix_{_TABLE}_tenant", table_name=_TABLE)
    op.drop_table(_TABLE)
```

En el core es igual, con `revision = "00NN"` y sin prefijo de pack.

## Excepción: tabla de SISTEMA (sin RLS)

Solo cuando la tabla se lee **antes** de que exista un tenant en contexto (login,
invitaciones, webhooks antes de resolver el tenant) o es un catálogo global. Ejemplos
vigentes: `webhook_events` (`0017`), `meta_app_subscriptions` (`0022`),
`admin_permissions` (`0035`), `connection_types` (`0038`) y `tenant_invitations` (`0042`).

Requisitos:
- La docstring empieza con **"Tabla de SISTEMA (sin RLS…)"** y explica por qué, con la
  referencia al ADR. El hook `guards.py` reconoce esa marca; sin ella bloquea el archivo.
- `grant` explícito y mínimo a `app_runtime` (solo `select` si es un catálogo).
- Si tiene `tenant_id`, el código que la consulta filtra por él explícitamente.
- Si la migración también crea tablas de negocio, mejor separarla en dos archivos: la
  marca exime al archivo entero.

## Cambios sobre tablas existentes

- Columna nueva: nullable o con `server_default`. El `NOT NULL` va en una migración
  posterior, después del backfill.
- Borrar una columna con datos son **dos fases**: deprecar → esperar un release →
  eliminar. Nunca en una sola.
- Índices sobre tablas grandes en producción: `postgresql_concurrently=True` dentro de
  `with op.get_context().autocommit_block():`.
- Nunca editar una migración ya aplicada en test o producción: se corrige con una nueva.

## Detente y pregunta si

- La tabla parece un catálogo global compartido entre tenants: probablemente es
  configuración del pack, no una tabla.
- Hay que quitar o cambiar `tenant_id`, RLS o la policy de una tabla existente.
- Se renombra una tabla o un ID `pack_*`: no se renombran nunca.
- La migración reescribe una tabla grande (lock largo) o borra datos.

## Antes de terminar

1. `uv run alembic heads` → un solo head en el core; en el pack, la cadena
   `down_revision` sin huecos.
2. Aplicar y revertir sobre el stack local (`make up`):
   - core: `make migrate`, `uv run alembic downgrade -1`, `make migrate`;
   - pack: `make migrate`, `(cd packages/pack-<x> && uv run alembic downgrade -1)`,
     `make migrate`.
   El esquema tiene que quedar igual.
3. Ninguna tabla con RLS y sin `FORCE` en la BD local:
   ```sql
   select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public' and c.relkind = 'r'
     and c.relrowsecurity and not c.relforcerowsecurity;
   ```
4. `AGENT_CORE_INTEGRATION=1 pytest tests/integration -q`, que incluye
   `test_tenant_isolation.py`. Si la tabla es nueva, agregar ahí el caso cross-tenant.
5. Esquema no trivial: pasar el agente `backend-design:schema-reviewer`. Siempre, antes
   de cerrar la tanda: el subagente `auditor-multitenant`.
