"""Planes e inscripción en suscripciones (SPEC-43, ADR-0177, D-55).

Crea el catálogo de planes (`pack_subscriptions_plans`) y el contador del número público
(`pack_subscriptions_counters` + `pack_subscriptions_next_seq`), y prepara las suscripciones
y los cargos para la inscripción: plan, número público, datos del alta, origen, motivo de
cancelación, el estado `pendiente_pago` y el tipo de cargo `inscripcion`.

**BORRA DATOS.** Esta migración elimina TODAS las filas de `pack_subscriptions_charges` y
`pack_subscriptions_subscriptions` (D-SPEC-43-4): hasta hoy el plan era una variante de
commerce, sus UUID no existen en el catálogo nuevo y los datos son de PRUEBA, descartables
por decisión del dueño (2026-10-27). No es reversible: `downgrade` deshace el esquema, no
devuelve las filas. Los negocios de test ya aprovisionados quedan huérfanos de su
suscripción. **Nunca contra una base con suscripciones reales**: antes de desplegar, el
runbook exige un conteo (`docs/runbooks/planes-e-inscripcion-despliegue.md`) y, si hay filas
reales, se aborta; en producción solo con el OK explícito del dueño y un respaldo restaurable.

Es ADITIVA respecto del camino viejo (`activate` + `plan_catalog_registry`, que viven hasta
HU-COM-17): `plan_id` y `numero_publico` quedan NULL-ables y `variante_id` /
`referencia_origen` se conservan. La contracción (`pack_subscriptions_0013`) quita las
columnas viejas y fija los NOT NULL junto con el PR que borra el código de planes de commerce.

Revision ID: pack_subscriptions_0010
Revises: pack_subscriptions_0009
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_subscriptions_0010"
down_revision = "pack_subscriptions_0009"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
PLANS = "pack_subscriptions_plans"
COUNTERS = "pack_subscriptions_counters"
SUBS = "pack_subscriptions_subscriptions"
CHARGES = "pack_subscriptions_charges"

ESTADOS = "('pendiente_pago','trialing','activa','pausada','vencida','cancelada')"
VIVOS_UNICO = "('pendiente_pago','trialing','activa','pausada','vencida')"


def _rls(tabla: str) -> None:
    op.execute(f"alter table {tabla} enable row level security")
    op.execute(f"alter table {tabla} force row level security")
    op.execute(
        f"create policy tenant_isolation on {tabla} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def upgrade() -> None:
    # ---------------------------------------------------------------- contador del número público
    op.create_table(
        COUNTERS,
        sa.Column("tenant_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    _rls(COUNTERS)
    op.execute(
        f"""
        create or replace function pack_subscriptions_next_seq(p_tenant uuid) returns integer
        language sql as $$
            insert into {COUNTERS} (tenant_id, last_seq)
            values (p_tenant, 1)
            on conflict (tenant_id) do update set last_seq = {COUNTERS}.last_seq + 1
            returning last_seq;
        $$;
        """
    )

    # ---------------------------------------------------------------- catálogo de planes
    # La tabla existió en 0001 (con `limites`) y se eliminó en 0002; esta es otra, con otro esquema.
    op.create_table(
        PLANS,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("codigo", sa.Text(), nullable=False),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("precio", sa.Numeric(14, 2), nullable=False),
        sa.Column("moneda", sa.Text(), nullable=False, server_default=sa.text("'COP'")),
        sa.Column("periodicidad", sa.Text(), nullable=False),
        sa.Column("trial_dias", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("entitlements", JSONB(), nullable=True, server_default=sa.text("'{}'::jsonb")),
        sa.Column("plan_plataforma", sa.Text(), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("orden", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("codigo ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$'", name="ck_pack_subscriptions_plans_codigo"),
        sa.CheckConstraint("char_length(nombre) between 1 and 120", name="ck_pack_subscriptions_plans_nombre"),
        sa.CheckConstraint("precio >= 0", name="ck_pack_subscriptions_plans_precio"),
        sa.CheckConstraint("char_length(moneda) = 3", name="ck_pack_subscriptions_plans_moneda"),
        sa.CheckConstraint(
            "periodicidad in ('mensual','trimestral','anual')", name="ck_pack_subscriptions_plans_periodicidad"
        ),
        sa.CheckConstraint("trial_dias between 0 and 365", name="ck_pack_subscriptions_plans_trial"),
        # D-16 en el esquema: solo un plan de $0 tiene prueba.
        sa.CheckConstraint("trial_dias = 0 or precio = 0", name="ck_pack_subscriptions_plans_d16"),
        sa.CheckConstraint(
            "entitlements is null or jsonb_typeof(entitlements) = 'object'",
            name="ck_pack_subscriptions_plans_entitlements",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_pack_subscriptions_plans_tenant_id"),
    )
    op.create_index(
        "uq_pack_subscriptions_plans_tenant_codigo",
        PLANS,
        ["tenant_id", "codigo"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.create_index("ix_pack_subscriptions_plans_tenant_activo", PLANS, ["tenant_id", "activo", "orden"])
    _rls(PLANS)

    # ---------------------------------------------------------------- borra los datos de prueba
    # FORCE RLS alcanza también al dueño de la tabla: sin `app.tenant_id` un DELETE no vería filas.
    # Se apaga RLS solo para el borrado y se restaura de inmediato (mismo patrón de pack-calendar 0006).
    for tabla in (CHARGES, SUBS):
        op.execute(f"alter table {tabla} disable row level security")
    op.execute(f"delete from {CHARGES}")
    op.execute(f"delete from {SUBS}")
    for tabla in (SUBS, CHARGES):
        op.execute(f"alter table {tabla} enable row level security")
        op.execute(f"alter table {tabla} force row level security")
    # Si la política quedó fuera por algún motivo, que la migración falle ruidosamente.
    op.execute(
        f"""
        do $$ begin
            if exists (select 1 from {SUBS}) or exists (select 1 from {CHARGES}) then
                raise exception 'pack_subscriptions_0010: quedaron filas tras el borrado';
            end if;
        end $$;
        """
    )

    # ---------------------------------------------------------------- suscripciones
    # `plan_id` ya existe desde 0001 como columna huérfana (sin FK desde 0002): se reutiliza.
    op.add_column(SUBS, sa.Column("numero_publico", sa.Text(), nullable=True))
    op.add_column(SUBS, sa.Column("datos_alta", JSONB(), nullable=True))
    op.add_column(SUBS, sa.Column("origen", sa.Text(), nullable=False, server_default=sa.text("'agente'")))
    op.add_column(SUBS, sa.Column("motivo_cancelacion", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_pack_subscriptions_subs_plan",
        SUBS,
        PLANS,
        ["tenant_id", "plan_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint("ck_pack_subscriptions_subs_estado", SUBS, f"estado in {ESTADOS}")
    op.create_check_constraint("ck_pack_subscriptions_subs_valor", SUBS, "valor_periodo >= 0")
    op.create_check_constraint(
        "ck_pack_subscriptions_subs_origen", SUBS, "origen in ('agente','panel')"
    )
    op.create_check_constraint(
        "ck_pack_subscriptions_subs_motivo_cancelacion",
        SUBS,
        "motivo_cancelacion is null or motivo_cancelacion in ('cliente','pago_no_recibido','reemplazada','admin')",
    )
    op.create_check_constraint(
        "ck_pack_subscriptions_subs_datos_alta",
        SUBS,
        "datos_alta is null or (jsonb_typeof(datos_alta) = 'object' and pg_column_size(datos_alta) <= 4096)",
    )
    op.create_check_constraint(
        "ck_pack_subscriptions_subs_pendiente_sin_periodo",
        SUBS,
        "estado <> 'pendiente_pago' or (proximo_cargo is null and periodo_actual_inicio is null)",
    )
    op.create_index(
        "uq_pack_subscriptions_subs_tenant_numero",
        SUBS,
        ["tenant_id", "numero_publico"],
        unique=True,
        postgresql_where=sa.text("numero_publico is not null"),
    )
    # Respaldo duro de la idempotencia de `inscribir`: un cliente no tiene dos vivas del mismo plan.
    op.create_index(
        "uq_pack_subscriptions_subs_vigente_cliente_plan",
        SUBS,
        ["tenant_id", "customer_id", "plan_id"],
        unique=True,
        postgresql_where=sa.text(f"estado in {VIVOS_UNICO} and deleted_at is null and plan_id is not null"),
    )
    op.create_index(
        "ix_pack_subscriptions_subs_pendientes",
        SUBS,
        ["tenant_id", "created_at"],
        postgresql_where=sa.text("estado = 'pendiente_pago' and deleted_at is null"),
    )

    # ---------------------------------------------------------------- cargos
    op.drop_constraint("ck_pack_subscriptions_charges_tipo", CHARGES, type_="check")
    op.create_check_constraint(
        "ck_pack_subscriptions_charges_tipo", CHARGES, "tipo in ('periodo','cambio_plan','inscripcion')"
    )
    op.create_check_constraint(
        "ck_pack_subscriptions_charges_estado", CHARGES, "estado in ('pendiente','pagado','rechazado','anulado')"
    )
    op.create_index(
        "uq_pack_subscriptions_charges_inscripcion",
        CHARGES,
        ["tenant_id", "subscription_id"],
        unique=True,
        postgresql_where=sa.text("tipo = 'inscripcion' and deleted_at is null"),
    )


def downgrade() -> None:
    # Deshace el esquema; las filas borradas por `upgrade` no vuelven.
    op.drop_index("uq_pack_subscriptions_charges_inscripcion", table_name=CHARGES)
    op.drop_constraint("ck_pack_subscriptions_charges_estado", CHARGES, type_="check")
    op.drop_constraint("ck_pack_subscriptions_charges_tipo", CHARGES, type_="check")
    # Los cargos de inscripción no existen en el esquema viejo.
    op.execute(f"alter table {CHARGES} disable row level security")
    op.execute(f"delete from {CHARGES} where tipo = 'inscripcion'")
    op.execute(f"alter table {CHARGES} enable row level security")
    op.execute(f"alter table {CHARGES} force row level security")
    op.create_check_constraint(
        "ck_pack_subscriptions_charges_tipo", CHARGES, "tipo in ('periodo', 'cambio_plan')"
    )

    op.drop_index("ix_pack_subscriptions_subs_pendientes", table_name=SUBS)
    op.drop_index("uq_pack_subscriptions_subs_vigente_cliente_plan", table_name=SUBS)
    op.drop_index("uq_pack_subscriptions_subs_tenant_numero", table_name=SUBS)
    for nombre in (
        "ck_pack_subscriptions_subs_pendiente_sin_periodo",
        "ck_pack_subscriptions_subs_datos_alta",
        "ck_pack_subscriptions_subs_motivo_cancelacion",
        "ck_pack_subscriptions_subs_origen",
        "ck_pack_subscriptions_subs_valor",
        "ck_pack_subscriptions_subs_estado",
        "fk_pack_subscriptions_subs_plan",
    ):
        op.drop_constraint(nombre, SUBS, type_="check" if nombre.startswith("ck_") else "foreignkey")
    for columna in ("motivo_cancelacion", "origen", "datos_alta", "numero_publico"):
        op.drop_column(SUBS, columna)

    op.execute("drop function if exists pack_subscriptions_next_seq(uuid)")
    op.drop_table(PLANS)
    op.drop_table(COUNTERS)
