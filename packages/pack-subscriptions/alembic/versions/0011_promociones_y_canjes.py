"""Promociones de plan y canjes (SPEC-43, HU-SUB-33, ADR-0177 §Promociones de plan).

Crea `pack_subscriptions_promociones` (el catálogo de promociones del negocio) y
`pack_subscriptions_canjes` (cada vez que un cliente canjea una, con la fotografía del descuento), y
agrega a `pack_subscriptions_charges` las columnas del descuento: `monto_bruto`, `descuento`,
`canje_id`, `periodo_n` y `consume_periodo`. `monto` sigue siendo SIEMPRE el neto que se cobra.

ADITIVA y reversible: no toca filas existentes (los cargos viejos quedan con las columnas nuevas en
NULL / false). Las FK compuestas `(tenant_id, id)` obligan a que `subscriptions` y `charges` tengan
un único `(tenant_id, id)` (la PK ya lo implica; aquí se declara). `charges.canje_id` es una
referencia lógica SIN FK: la FK vive solo en `canjes.charge_id`, así cargo y canje se crean en orden
(cargo → canje → `UPDATE charges SET canje_id`) sin una FK circular.

Revision ID: pack_subscriptions_0011
Revises: pack_subscriptions_0010
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_subscriptions_0011"
down_revision = "pack_subscriptions_0010"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
PLANS = "pack_subscriptions_plans"
SUBS = "pack_subscriptions_subscriptions"
CHARGES = "pack_subscriptions_charges"
PROMOS = "pack_subscriptions_promociones"
CANJES = "pack_subscriptions_canjes"

ESTADOS_CANJE = "('reservado','confirmado','agotado','terminado','liberado')"


def _rls(tabla: str) -> None:
    op.execute(f"alter table {tabla} enable row level security")
    op.execute(f"alter table {tabla} force row level security")
    op.execute(
        f"create policy tenant_isolation on {tabla} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def upgrade() -> None:
    # ---------------------------------------------------------------- únicos para las FK compuestas
    op.create_unique_constraint("uq_pack_subscriptions_subs_tenant_id", SUBS, ["tenant_id", "id"])
    op.create_unique_constraint("uq_pack_subscriptions_charges_tenant_id", CHARGES, ["tenant_id", "id"])

    # ---------------------------------------------------------------- promociones
    op.create_table(
        PROMOS,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("etiqueta", sa.Text(), nullable=False),
        # Secreto de cupón: minúsculas. NULL = promoción sin código (no se canjea por código).
        sa.Column("codigo", sa.Text(), nullable=True),
        sa.Column("tipo_descuento", sa.Text(), nullable=False),
        sa.Column("valor", sa.Numeric(14, 2), nullable=False),
        sa.Column("periodos", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("plan_id", UUID(as_uuid=True), nullable=True),  # NULL = cualquier plan de pago
        sa.Column("vigente_desde", sa.DateTime(timezone=True), nullable=True),
        sa.Column("vigente_hasta", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_canjes", sa.Integer(), nullable=True),
        sa.Column("canjes_usados", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("activa", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("char_length(etiqueta) between 1 and 80", name="ck_pack_subscriptions_promos_etiqueta"),
        sa.CheckConstraint(
            "codigo is null or codigo ~ '^[a-z0-9][a-z0-9_-]{2,39}$'", name="ck_pack_subscriptions_promos_codigo"
        ),
        sa.CheckConstraint(
            "tipo_descuento in ('porcentaje','monto')", name="ck_pack_subscriptions_promos_tipo"
        ),
        sa.CheckConstraint("valor > 0", name="ck_pack_subscriptions_promos_valor"),
        # 1 a 99 %: el 0 no descuenta y el 100 no es un cobro (el neto no puede ser 0).
        sa.CheckConstraint(
            "tipo_descuento <> 'porcentaje' or valor between 1 and 99", name="ck_pack_subscriptions_promos_porcentaje"
        ),
        sa.CheckConstraint("periodos between 1 and 12", name="ck_pack_subscriptions_promos_periodos"),
        # Un monto fijo se descuenta una sola vez: con N >= 2 no es comparable entre períodos.
        sa.CheckConstraint(
            "tipo_descuento = 'porcentaje' or periodos = 1", name="ck_pack_subscriptions_promos_monto_un_periodo"
        ),
        sa.CheckConstraint(
            "vigente_desde is null or vigente_hasta is null or vigente_hasta > vigente_desde",
            name="ck_pack_subscriptions_promos_vigencia",
        ),
        sa.CheckConstraint("max_canjes is null or max_canjes > 0", name="ck_pack_subscriptions_promos_max"),
        sa.CheckConstraint(
            "canjes_usados >= 0 and (max_canjes is null or canjes_usados <= max_canjes)",
            name="ck_pack_subscriptions_promos_usados",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_pack_subscriptions_promos_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "plan_id"], [f"{PLANS}.tenant_id", f"{PLANS}.id"],
            name="fk_pack_subscriptions_promos_plan", ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_pack_subscriptions_promos_tenant_codigo",
        PROMOS,
        ["tenant_id", "codigo"],
        unique=True,
        postgresql_where=sa.text("codigo is not null and deleted_at is null"),
    )
    op.create_index("ix_pack_subscriptions_promos_tenant_activa", PROMOS, ["tenant_id", "activa", "created_at"])
    _rls(PROMOS)

    # ---------------------------------------------------------------- canjes
    op.create_table(
        CANJES,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("promocion_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=False),  # FK lógica al core
        sa.Column("subscription_id", UUID(as_uuid=True), nullable=False),
        sa.Column("charge_id", UUID(as_uuid=True), nullable=False),
        # `<tipo>:<HMAC>` del antiabuso del core (documento o, sin él, teléfono): nunca el dato en claro.
        sa.Column("huella", sa.Text(), nullable=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default=sa.text("'reservado'")),
        # Fotografía congelada de la promoción al canjear: editarla después no cambia lo ya canjeado.
        sa.Column("promocion", JSONB(), nullable=False),
        sa.Column("periodos_total", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(f"estado in {ESTADOS_CANJE}", name="ck_pack_subscriptions_canjes_estado"),
        sa.CheckConstraint("periodos_total between 1 and 12", name="ck_pack_subscriptions_canjes_periodos"),
        sa.CheckConstraint("jsonb_typeof(promocion) = 'object'", name="ck_pack_subscriptions_canjes_promocion"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_pack_subscriptions_canjes_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "promocion_id"], [f"{PROMOS}.tenant_id", f"{PROMOS}.id"],
            name="fk_pack_subscriptions_canjes_promocion", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "subscription_id"], [f"{SUBS}.tenant_id", f"{SUBS}.id"],
            name="fk_pack_subscriptions_canjes_sub", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "charge_id"], [f"{CHARGES}.tenant_id", f"{CHARGES}.id"],
            name="fk_pack_subscriptions_canjes_charge", ondelete="RESTRICT",
        ),
    )
    # Un canje por cliente, por persona (huella) y por cargo. Solo cuenta lo que no se liberó: un cargo
    # anulado devuelve el cupo y el derecho a volver a canjear; agotado y terminado SIGUEN contando.
    donde = sa.text("estado <> 'liberado'")
    op.create_index(
        "uq_pack_subscriptions_canjes_cliente", CANJES, ["tenant_id", "promocion_id", "customer_id"],
        unique=True, postgresql_where=donde,
    )
    op.create_index(
        "uq_pack_subscriptions_canjes_huella", CANJES, ["tenant_id", "promocion_id", "huella"],
        unique=True, postgresql_where=sa.text("estado <> 'liberado' and huella is not null"),
    )
    op.create_index(
        "uq_pack_subscriptions_canjes_cargo", CANJES, ["tenant_id", "charge_id"],
        unique=True, postgresql_where=donde,
    )
    op.create_index("ix_pack_subscriptions_canjes_promo", CANJES, ["tenant_id", "promocion_id", "created_at"])
    op.create_index(
        "ix_pack_subscriptions_canjes_reservados", CANJES, ["tenant_id", "created_at"],
        postgresql_where=sa.text("estado = 'reservado'"),
    )
    _rls(CANJES)

    # ---------------------------------------------------------------- columnas del descuento en los cargos
    op.add_column(CHARGES, sa.Column("monto_bruto", sa.Numeric(14, 2), nullable=True))
    op.add_column(CHARGES, sa.Column("descuento", JSONB(), nullable=True))
    op.add_column(CHARGES, sa.Column("canje_id", UUID(as_uuid=True), nullable=True))  # referencia lógica, sin FK
    op.add_column(CHARGES, sa.Column("periodo_n", sa.SmallInteger(), nullable=True))
    op.add_column(CHARGES, sa.Column("consume_periodo", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.create_check_constraint(
        "ck_pack_subscriptions_charges_descuento_par", CHARGES, "(monto_bruto is null) = (descuento is null)"
    )
    # `monto` es el neto: nunca 0 ni mayor que el bruto cuando hay descuento.
    op.create_check_constraint(
        "ck_pack_subscriptions_charges_neto", CHARGES, "monto_bruto is null or (monto > 0 and monto <= monto_bruto)"
    )
    op.create_check_constraint(
        "ck_pack_subscriptions_charges_periodo_n", CHARGES, "periodo_n is null or periodo_n between 1 and 12"
    )

    # Mínimo privilegio: el runtime no borra (el borrado de promociones es lógico y los canjes no se borran).
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON {PROMOS}, {CANJES} FROM app_runtime;
                GRANT SELECT, INSERT, UPDATE ON {PROMOS} TO app_runtime;
                GRANT SELECT, INSERT, UPDATE ON {CANJES} TO app_runtime;
            END IF;
        END $do$;
        """
    )
    # La migración falla ruidosamente si una tabla nueva quedó sin RLS habilitada y forzada o sin su policy.
    op.execute(
        f"""
        do $$
        declare t text;
        begin
            foreach t in array array['{PROMOS}', '{CANJES}'] loop
                if not exists (
                    select 1 from pg_class
                    where relname = t and relkind = 'r' and relrowsecurity and relforcerowsecurity
                ) then
                    raise exception 'pack_subscriptions_0011: % sin RLS habilitada y forzada', t;
                end if;
                if not exists (select 1 from pg_policies where tablename = t and policyname = 'tenant_isolation') then
                    raise exception 'pack_subscriptions_0011: % sin policy tenant_isolation', t;
                end if;
            end loop;
        end $$;
        """
    )


def downgrade() -> None:
    # Los canjes y las promociones no existen en el esquema anterior: se pierden con las tablas.
    for nombre in (
        "ck_pack_subscriptions_charges_periodo_n",
        "ck_pack_subscriptions_charges_neto",
        "ck_pack_subscriptions_charges_descuento_par",
    ):
        op.drop_constraint(nombre, CHARGES, type_="check")
    for columna in ("consume_periodo", "periodo_n", "canje_id", "descuento", "monto_bruto"):
        op.drop_column(CHARGES, columna)
    op.drop_table(CANJES)
    op.drop_table(PROMOS)
    op.drop_constraint("uq_pack_subscriptions_charges_tenant_id", CHARGES, type_="unique")
    op.drop_constraint("uq_pack_subscriptions_subs_tenant_id", SUBS, type_="unique")
