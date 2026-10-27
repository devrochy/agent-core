"""Varias agendas por negocio: `pack_calendar_calendars` + `calendar_id` (HU-CAL-08, ADR-0170).

- Tabla `pack_calendar_calendars`: cada agenda tiene el nombre que el asistente le dice
  al cliente. Una conexión de Google = una agenda (`connection_id`, FK lógica: ningún pack
  hace FK al core). Sin conexión es una agenda interna.
- `calendar_id` (nullable) en citas y bloqueos, con backfill a la agenda por defecto.
  `NULL` se trata como «la agenda por defecto» hasta la migración «contract» (NOT NULL); un
  trigger BEFORE INSERT/UPDATE en las citas rellena el `NULL` con la agenda por defecto.
- La exclusión de solape pasa a ser por agenda:
  `(tenant_id, coalesce(calendar_id, nil), tstzrange)`. Con una sola agenda es la misma
  garantía que antes (el trigger evita que `NULL` y el id de la principal se traten distinto). La clave nueva es más fina que la vieja: no hace falta revisar solapes.

`CalendarConfig` vive cifrada en `plugin_configs`, así que el SQL no puede leer `source`,
`timezone` ni `business_hours`: la agenda por defecto nace con horario y zona en `NULL`
(= hereda del config global) y `connection_id` solo se llena cuando hay evidencia legible
(una cita con `google_event_id`). Lo demás lo completa `ensure_default()` en ejecución.

El backfill corre sin `tenant_scope`: con RLS forzada solo toca filas si el rol de
migraciones tiene BYPASSRLS (ADR-0094 §5; test y producción lo tienen). Si no, no inserta
nada y `ensure_default()` crea la agenda por defecto al primer uso.

La exclusión nueva se crea antes de borrar la vieja y todo va en una transacción con
`lock_timeout` (el ADD COLUMN y el ADD CONSTRAINT toman ACCESS EXCLUSIVE y lo retienen hasta el commit: la
construcción del GiST y el backfill corren con la tabla bloqueada; `pack_calendar_appointments`
es chica, verificar el conteo antes de aplicar en producción). El downgrade borra las agendas
(nombres, alias, horarios) y reasigna todo a «sin agenda»: un nuevo upgrade vuelve a mandar todo
a la principal.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_CAL = "pack_calendar_calendars"
_APPTS = "pack_calendar_appointments"
_BLOCKS = "pack_calendar_blocks"
_VIEJA = "ex_pack_calendar_appointments_sin_solape"
_NUEVA = "ex_pack_calendar_appointments_sin_solape_cal"
_FN = "fn_pack_calendar_appt_calendar_default"
_TRG = "trg_pack_calendar_appt_calendar_default"
_NIL = "'00000000-0000-0000-0000-000000000000'::uuid"
_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        _CAL,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("aliases", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("color", sa.Text(), nullable=False, server_default="cat-1"),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("business_hours", sa.Text(), nullable=True),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=True),
        sa.Column("google_calendar_id", sa.Text(), nullable=False, server_default="primary"),
        sa.Column("responsable_admin_id", UUID(as_uuid=True), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "color in ('cat-1', 'cat-2', 'cat-3', 'cat-4')", name="ck_pack_calendar_calendars_color"
        ),
        sa.CheckConstraint("char_length(name) between 1 and 60", name="ck_pack_calendar_calendars_name"),
    )
    op.create_index("ix_pack_calendar_calendars_tenant", _CAL, ["tenant_id", "sort_order"])
    op.create_index(
        "uq_pack_calendar_calendars_default",
        _CAL,
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default and deleted_at is null"),
    )
    op.create_index(
        "uq_pack_calendar_calendars_connection",
        _CAL,
        ["tenant_id", "connection_id"],
        unique=True,
        postgresql_where=sa.text("connection_id is not null and deleted_at is null"),
    )
    op.create_index(
        "uq_pack_calendar_calendars_name",
        _CAL,
        ["tenant_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.execute(f"alter table {_CAL} enable row level security")
    op.execute(f"alter table {_CAL} force row level security")
    op.execute(
        f"create policy tenant_isolation on {_CAL} "
        f"using (tenant_id = {_RLS}) with check (tenant_id = {_RLS})"
    )
    op.execute(f"grant select, insert, update, delete on {_CAL} to app_runtime")

    op.add_column(_APPTS, sa.Column("calendar_id", UUID(as_uuid=True), nullable=True))
    op.add_column(_BLOCKS, sa.Column("calendar_id", UUID(as_uuid=True), nullable=True))

    # Backfill: una agenda por defecto por negocio que ya usa el pack (idempotente).
    op.execute(
        f"""
        insert into {_CAL} (id, tenant_id, name, is_default, sort_order)
        select gen_random_uuid(), t.tenant_id, 'Agenda principal', true, 0
        from (
          select tenant_id from {_APPTS}
          union select tenant_id from {_BLOCKS}
          union select tenant_id from pack_calendar_appointment_types
          union select tenant_id from plugin_configs where plugin_id = 'pack_calendar'
        ) t
        where t.tenant_id is not null
          and not exists (
            select 1 from {_CAL} c where c.tenant_id = t.tenant_id and c.is_default and c.deleted_at is null
          )
        """
    )
    op.execute(
        f"""
        update {_APPTS} a set calendar_id = c.id
        from {_CAL} c
        where c.tenant_id = a.tenant_id and c.is_default and c.deleted_at is null and a.calendar_id is null
        """
    )
    op.execute(
        f"""
        update {_BLOCKS} b set calendar_id = c.id
        from {_CAL} c
        where c.tenant_id = b.tenant_id and c.is_default and c.deleted_at is null and b.calendar_id is null
        """
    )

    # Mientras `calendar_id` siga siendo nullable (hasta el «contract»), una cita nueva sin agenda
    # (pods viejos durante el despliegue, o un negocio sin backfill) cae en la agenda por defecto:
    # si no, `NULL` y el id de la principal no chocarían en la exclusión y se podría reservar doble.
    op.execute(
        f"""
        create or replace function {_FN}() returns trigger language plpgsql as $$
        begin
          if new.calendar_id is null then
            select c.id into new.calendar_id from {_CAL} c
             where c.tenant_id = new.tenant_id and c.is_default and c.deleted_at is null limit 1;
          end if;
          return new;
        end $$
        """
    )
    op.execute(
        f"create trigger {_TRG} before insert or update of calendar_id on {_APPTS} "
        f"for each row execute function {_FN}()"
    )
    op.create_index("ix_pack_calendar_appts_calendar_start", _APPTS, ["tenant_id", "calendar_id", "start_at"])
    op.create_index("ix_pack_calendar_blocks_calendar_start", _BLOCKS, ["tenant_id", "calendar_id", "start_at"])

    # Exclusión por agenda: la nueva (más fina) entra antes de soltar la vieja.
    op.execute(
        f"""
        alter table {_APPTS}
        add constraint {_NUEVA}
        exclude using gist (
          tenant_id with =,
          (coalesce(calendar_id, {_NIL})) with =,
          tstzrange(start_at, end_at) with &&
        )
        where (status = 'scheduled' and deleted_at is null and google_event_id is null)
        """
    )
    op.execute(f"alter table {_APPTS} drop constraint if exists {_VIEJA}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # La exclusión vieja es más gruesa: si ya hay citas que solapan entre agendas, fallar claro.
    conflictos = (
        op.get_bind()
        .execute(
            sa.text(
                f"""
                select count(*) from {_APPTS} a join {_APPTS} b
                  on a.tenant_id = b.tenant_id and a.id < b.id
                 and a.status = 'scheduled' and b.status = 'scheduled'
                 and a.deleted_at is null and b.deleted_at is null
                 and a.google_event_id is null and b.google_event_id is null
                 and tstzrange(a.start_at, a.end_at) && tstzrange(b.start_at, b.end_at)
                """
            )
        )
        .scalar()
    )
    if conflictos:
        raise RuntimeError(
            f"No se puede revertir 0006: hay {conflictos} pares de citas que solapan entre "
            "agendas distintas. Muévelas o cancélalas y vuelve a intentar."
        )
    op.execute(
        f"""
        alter table {_APPTS}
        add constraint {_VIEJA}
        exclude using gist (tenant_id with =, tstzrange(start_at, end_at) with &&)
        where (status = 'scheduled' and deleted_at is null and google_event_id is null)
        """
    )
    op.execute(f"alter table {_APPTS} drop constraint if exists {_NUEVA}")
    op.execute(f"drop trigger if exists {_TRG} on {_APPTS}")
    op.execute(f"drop function if exists {_FN}()")
    op.drop_index("ix_pack_calendar_blocks_calendar_start", table_name=_BLOCKS)
    op.drop_index("ix_pack_calendar_appts_calendar_start", table_name=_APPTS)
    op.drop_column(_BLOCKS, "calendar_id")
    op.drop_column(_APPTS, "calendar_id")
    op.execute(f"drop policy if exists tenant_isolation on {_CAL}")
    op.execute(f"alter table {_CAL} no force row level security")
    op.execute(f"alter table {_CAL} disable row level security")
    op.drop_index("uq_pack_calendar_calendars_name", table_name=_CAL)
    op.drop_index("uq_pack_calendar_calendars_connection", table_name=_CAL)
    op.drop_index("uq_pack_calendar_calendars_default", table_name=_CAL)
    op.drop_index("ix_pack_calendar_calendars_tenant", table_name=_CAL)
    op.drop_table(_CAL)
