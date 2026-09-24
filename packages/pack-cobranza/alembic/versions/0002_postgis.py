"""PostGIS para pack-cobranza (ADR-0058): extensión + geografía de clientes.

Agrega `geom geography(Point,4326)` a `pack_cobranza_clientes`, un trigger que
la mantiene en sync con `lat`/`lng` (así el modelo no necesita `geoalchemy2`) y
un índice GIST para consultas geoespaciales.

Revision ID: pack_cobranza_0002
Revises: pack_cobranza_0001
Create Date: 2026-09-24
"""

from __future__ import annotations

from alembic import op

revision = "pack_cobranza_0002"
down_revision = "pack_cobranza_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("create extension if not exists postgis")
    op.execute("alter table pack_cobranza_clientes add column if not exists geom geography(Point, 4326)")
    op.execute(
        """
        create or replace function pack_cobranza_set_geom() returns trigger as $$
        begin
            if new.lat is not null and new.lng is not null then
                new.geom := ST_SetSRID(ST_MakePoint(new.lng, new.lat), 4326)::geography;
            else
                new.geom := null;
            end if;
            return new;
        end;
        $$ language plpgsql;
        """
    )
    op.execute(
        "create trigger pack_cobranza_clientes_geom before insert or update "
        "on pack_cobranza_clientes for each row execute function pack_cobranza_set_geom()"
    )
    op.execute(
        "create index if not exists ix_pack_cobranza_clientes_geom "
        "on pack_cobranza_clientes using gist (geom)"
    )


def downgrade() -> None:
    op.execute("drop index if exists ix_pack_cobranza_clientes_geom")
    op.execute("drop trigger if exists pack_cobranza_clientes_geom on pack_cobranza_clientes")
    op.execute("drop function if exists pack_cobranza_set_geom()")
    op.execute("alter table pack_cobranza_clientes drop column if exists geom")
