"""Esquema inicial de pack-cobranza alineado a app-cobranza develop (ADR-0060).

Modelo: **Propietario → Cartera → Cliente → Préstamo**, con Gestor de cartera,
cartera_config, caja/ajustes, inyecciones, gastos+evidencias, abonos, permisos
y liquidaciones. Greenfield (ADR-0059): `tenant_id NOT NULL` desde el arranque,
RLS + policy de aislamiento e índices compuestos que empiezan por `tenant_id`.
La geografía (PostGIS) se agrega en la migración 0002.

Revision ID: pack_cobranza_0001
Revises:
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_cobranza_0001"
down_revision = None
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_NUM = sa.Numeric(14, 2)
_UUID = UUID(as_uuid=True)


def _id() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", _UUID, nullable=False)


def _audit() -> list[sa.Column]:
    return [
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    ]


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def upgrade() -> None:
    # --- identidad ---
    # El propietario ES el tenant (ADR-0062): no hay tabla de propietarios.
    op.create_table(
        "pack_cobranza_gestores",
        _id(), _tenant(),
        sa.Column("usuario", sa.Text(), nullable=True),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("apellido", sa.Text(), nullable=True),
        sa.Column("correo", sa.Text(), nullable=True),
        sa.Column("telefono", sa.Text(), nullable=True),
        sa.Column("codigo", sa.Text(), nullable=True),
        sa.Column("estatus", sa.Text(), nullable=False, server_default="activo"),
        sa.Column("custom", JSONB(), nullable=True),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_gestores_tenant_estatus", "pack_cobranza_gestores", ["tenant_id", "estatus"])
    _rls("pack_cobranza_gestores")

    op.create_table(
        "pack_cobranza_gestor_permisos",
        _id(), _tenant(),
        sa.Column("gestor_id", _UUID, nullable=False),
        sa.Column("permiso", sa.Text(), nullable=False),
        sa.Column("habilitado", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_pack_cobranza_gestor_permiso", "pack_cobranza_gestor_permisos",
        ["tenant_id", "gestor_id", "permiso"], unique=True,
    )
    _rls("pack_cobranza_gestor_permisos")

    # --- cartera ---
    op.create_table(
        "pack_cobranza_carteras",
        _id(), _tenant(),
        sa.Column("gestor_id", _UUID, nullable=True),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("tipo_interes", sa.Numeric(6, 3), nullable=False, server_default="0"),
        sa.Column("num_cuotas", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("moneda", sa.Text(), nullable=False, server_default="COP"),
        sa.Column("costo_cobro", _NUM, nullable=False, server_default="0"),
        sa.Column("estatus", sa.Text(), nullable=False, server_default="activo"),
        sa.Column("custom", JSONB(), nullable=True),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_carteras_tenant_estatus", "pack_cobranza_carteras", ["tenant_id", "estatus"])
    _rls("pack_cobranza_carteras")

    _b = sa.Boolean()
    op.create_table(
        "pack_cobranza_cartera_config",
        _id(), _tenant(),
        sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("cuotas_minimas_prestamo", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cuotas_atraso_umbral", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manejo_cupo_activo", _b, nullable=False, server_default="false"),
        sa.Column("cupo_default", _NUM, nullable=False, server_default="0"),
        sa.Column("recargo_activo", _b, nullable=False, server_default="false"),
        sa.Column("bloquear_cambio_interes", _b, nullable=False, server_default="false"),
        sa.Column("comision_activa", _b, nullable=False, server_default="false"),
        sa.Column("comision_porcentaje", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("mostrar_fecha_ultima_liquidada", _b, nullable=False, server_default="true"),
        sa.Column("mostrar_caja", _b, nullable=False, server_default="true"),
        sa.Column("mostrar_cobrado_liquidada", _b, nullable=False, server_default="true"),
        sa.Column("mostrar_prestamos", _b, nullable=False, server_default="true"),
        sa.Column("mostrar_cobro_estimado", _b, nullable=False, server_default="true"),
        sa.Column("ocultar_cartera", _b, nullable=False, server_default="false"),
        sa.Column("eliminar_prestamos_apk", _b, nullable=False, server_default="false"),
        sa.Column("eliminar_pagos_apk", _b, nullable=False, server_default="false"),
        sa.Column("eliminar_abonos_apk", _b, nullable=False, server_default="false"),
        sa.Column("eliminar_gastos_apk", _b, nullable=False, server_default="false"),
        sa.Column("eliminar_inyeccion_apk", _b, nullable=False, server_default="false"),
        sa.Column("registrar_inyeccion_apk", _b, nullable=False, server_default="false"),
        sa.Column("generar_reportes_apk", _b, nullable=False, server_default="false"),
        sa.Column("reconocimiento_facial_activo", _b, nullable=False, server_default="false"),
        sa.Column("registro_documento_cliente", _b, nullable=False, server_default="false"),
        sa.Column("bloqueo_automatico_clientes", _b, nullable=False, server_default="false"),
        sa.Column("borrar_clientes_sin_deuda", _b, nullable=False, server_default="false"),
        sa.Column("permitir_cambio_fecha_prestamo", _b, nullable=False, server_default="false"),
        sa.Column("dias_no_laborables", sa.Text(), nullable=False, server_default="solo_domingos"),
        sa.Column("periodo_liquidacion", sa.Text(), nullable=False, server_default="diario"),
        sa.Column("dias_anticipacion_notificacion", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("aviso_dia_cobro", _b, nullable=False, server_default="false"),
        sa.Column("umbral_mora_notificacion", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ruta_habilitada", _b, nullable=False, server_default="false"),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_pack_cobranza_cartera_config", "pack_cobranza_cartera_config", ["tenant_id", "cartera_id"], unique=True)
    _rls("pack_cobranza_cartera_config")

    op.create_table(
        "pack_cobranza_cajas",
        _id(), _tenant(),
        sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("saldo_inicial", _NUM, nullable=False, server_default="0"),
        sa.Column("saldo_actual", _NUM, nullable=False, server_default="0"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_pack_cobranza_caja", "pack_cobranza_cajas", ["tenant_id", "cartera_id"], unique=True)
    _rls("pack_cobranza_cajas")

    op.create_table(
        "pack_cobranza_caja_ajustes",
        _id(), _tenant(),
        sa.Column("caja_id", _UUID, nullable=False),
        sa.Column("valor_anterior", _NUM, nullable=False, server_default="0"),
        sa.Column("valor_nuevo", _NUM, nullable=False, server_default="0"),
        sa.Column("motivo", sa.Text(), nullable=False),
        sa.Column("actor_rol", sa.Text(), nullable=True),
        sa.Column("actor_id", _UUID, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pack_cobranza_caja_ajustes_tenant_caja", "pack_cobranza_caja_ajustes", ["tenant_id", "caja_id"])
    _rls("pack_cobranza_caja_ajustes")

    op.create_table(
        "pack_cobranza_inyecciones",
        _id(), _tenant(),
        sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("valor", _NUM, nullable=False),
        sa.Column("comentario", sa.Text(), nullable=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="activa"),
        sa.Column("fecha_hora", _TS, nullable=False, server_default=sa.text("now()")),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_inyecciones_tenant_cartera", "pack_cobranza_inyecciones", ["tenant_id", "cartera_id"])
    _rls("pack_cobranza_inyecciones")

    op.create_table(
        "pack_cobranza_gastos",
        _id(), _tenant(),
        sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=False),
        sa.Column("valor", _NUM, nullable=False),
        sa.Column("creado_por", _UUID, nullable=True),
        sa.Column("aprobado", _b, nullable=False, server_default="false"),
        sa.Column("aprobado_por", _UUID, nullable=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="activo"),
        sa.Column("fecha_hora", _TS, nullable=False, server_default=sa.text("now()")),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_gastos_tenant_cartera", "pack_cobranza_gastos", ["tenant_id", "cartera_id"])
    _rls("pack_cobranza_gastos")

    op.create_table(
        "pack_cobranza_gasto_evidencias",
        _id(), _tenant(),
        sa.Column("gasto_id", _UUID, nullable=False),
        sa.Column("ruta_archivo", sa.Text(), nullable=False),
        sa.Column("nombre_original", sa.Text(), nullable=True),
        sa.Column("mimetype", sa.Text(), nullable=True),
        sa.Column("tamano", sa.Integer(), nullable=True),
        sa.Column("creado_por_rol", sa.Text(), nullable=True),
        sa.Column("creado_por_id", _UUID, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pack_cobranza_gasto_evidencias_tenant_gasto", "pack_cobranza_gasto_evidencias", ["tenant_id", "gasto_id"])
    _rls("pack_cobranza_gasto_evidencias")

    # --- clientes ---
    op.create_table(
        "pack_cobranza_clientes",
        _id(), _tenant(),
        sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("customer_id", _UUID, nullable=True),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("apellido", sa.Text(), nullable=True),
        sa.Column("negocio", sa.Text(), nullable=True),
        sa.Column("telefono_whatsapp", sa.Text(), nullable=True),
        sa.Column("tipo_documento", sa.Text(), nullable=True),
        sa.Column("numero_documento", sa.Text(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("lat_domicilio", sa.Float(), nullable=True),
        sa.Column("lng_domicilio", sa.Float(), nullable=True),
        sa.Column("tope_maximo_deuda", _NUM, nullable=True),
        sa.Column("color_riesgo", sa.Text(), nullable=False, server_default="blanco"),
        sa.Column("estatus", sa.Text(), nullable=False, server_default="activo"),
        sa.Column("custom", JSONB(), nullable=True),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_clientes_tenant_cartera", "pack_cobranza_clientes", ["tenant_id", "cartera_id"])
    op.create_index("ix_pack_cobranza_clientes_tenant_customer", "pack_cobranza_clientes", ["tenant_id", "customer_id"])
    _rls("pack_cobranza_clientes")

    op.create_table(
        "pack_cobranza_prestamos",
        _id(), _tenant(),
        sa.Column("cliente_id", _UUID, nullable=False),
        sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("valor", _NUM, nullable=False),
        sa.Column("saldo", _NUM, nullable=False, server_default="0"),
        sa.Column("num_cuotas", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tipo_interes", sa.Numeric(6, 3), nullable=False, server_default="0"),
        sa.Column("dias_entre_cuotas", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fecha_otorgado", sa.Date(), nullable=False),
        sa.Column("fiador_nombre", sa.Text(), nullable=True),
        sa.Column("fiador_apellido", sa.Text(), nullable=True),
        sa.Column("fiador_documento", sa.Text(), nullable=True),
        sa.Column("fiador_telefono", sa.Text(), nullable=True),
        sa.Column("estatus", sa.Text(), nullable=False, server_default="vigente"),
        sa.Column("custom", JSONB(), nullable=True),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_prestamos_tenant_cliente", "pack_cobranza_prestamos", ["tenant_id", "cliente_id"])
    _rls("pack_cobranza_prestamos")

    op.create_table(
        "pack_cobranza_cuotas",
        _id(), _tenant(),
        sa.Column("prestamo_id", _UUID, nullable=False),
        sa.Column("numero_cuota", sa.Integer(), nullable=False),
        sa.Column("valor_esperado", _NUM, nullable=False),
        sa.Column("fecha_vencimiento", sa.Date(), nullable=False),
        sa.Column("estatus", sa.Text(), nullable=False, server_default="pendiente"),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_cuotas_tenant_prestamo", "pack_cobranza_cuotas", ["tenant_id", "prestamo_id"])
    _rls("pack_cobranza_cuotas")

    op.create_table(
        "pack_cobranza_pagos",
        _id(), _tenant(),
        sa.Column("cliente_id", _UUID, nullable=False),
        sa.Column("prestamo_id", _UUID, nullable=False),
        sa.Column("cuota_id", _UUID, nullable=True),
        sa.Column("valor", _NUM, nullable=False),
        sa.Column("metodo_pago", sa.Text(), nullable=False, server_default="efectivo"),
        sa.Column("registrado_por", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("fecha_hora", _TS, nullable=False, server_default=sa.text("now()")),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_pagos_tenant_cliente", "pack_cobranza_pagos", ["tenant_id", "cliente_id"])
    op.create_index("uq_pack_cobranza_pagos_tenant_idem", "pack_cobranza_pagos", ["tenant_id", "idempotency_key"], unique=True)
    _rls("pack_cobranza_pagos")

    op.create_table(
        "pack_cobranza_abonos",
        _id(), _tenant(),
        sa.Column("prestamo_id", _UUID, nullable=False),
        sa.Column("cliente_id", _UUID, nullable=False),
        sa.Column("visita_id", _UUID, nullable=True),
        sa.Column("valor", _NUM, nullable=False),
        sa.Column("metodo_pago", sa.Text(), nullable=False, server_default="efectivo"),
        sa.Column("registrado_por", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("fecha_hora", _TS, nullable=False, server_default=sa.text("now()")),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_abonos_tenant_cliente", "pack_cobranza_abonos", ["tenant_id", "cliente_id"])
    op.create_index("uq_pack_cobranza_abonos_tenant_idem", "pack_cobranza_abonos", ["tenant_id", "idempotency_key"], unique=True)
    _rls("pack_cobranza_abonos")

    op.create_table(
        "pack_cobranza_visitas",
        _id(), _tenant(),
        sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("cliente_id", _UUID, nullable=False),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("resultado", sa.Text(), nullable=False),
        sa.Column("motivo_no_pago", sa.Text(), nullable=True),
        sa.Column("valor_pagado", _NUM, nullable=True),
        sa.Column("metodo_pago", sa.Text(), nullable=True),
        sa.Column("creado_por", sa.Text(), nullable=True),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_visitas_tenant_cartera", "pack_cobranza_visitas", ["tenant_id", "cartera_id", "fecha"])
    _rls("pack_cobranza_visitas")

    op.create_table(
        "pack_cobranza_promesas",
        _id(), _tenant(),
        sa.Column("prestamo_id", _UUID, nullable=False),
        sa.Column("cliente_id", _UUID, nullable=False),
        sa.Column("fecha_prometida", sa.Date(), nullable=False),
        sa.Column("valor_prometido", _NUM, nullable=False),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("tipo", sa.Text(), nullable=False, server_default="promesa"),
        sa.Column("creado_por", sa.Text(), nullable=False, server_default="ia"),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_promesas_tenant_prestamo", "pack_cobranza_promesas", ["tenant_id", "prestamo_id"])
    op.create_index("uq_pack_cobranza_promesas_tenant_idem", "pack_cobranza_promesas", ["tenant_id", "idempotency_key"], unique=True)
    _rls("pack_cobranza_promesas")

    op.create_table(
        "pack_cobranza_reglas_negociacion",
        sa.Column("tenant_id", _UUID, primary_key=True),
        sa.Column("max_dias_prorroga", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("min_abono_pct", sa.Numeric(5, 2), nullable=False, server_default="10"),
        sa.Column("max_reprogramaciones", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("umbral_saldo_autonomo", _NUM, nullable=False, server_default="0"),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    _rls("pack_cobranza_reglas_negociacion")

    op.create_table(
        "pack_cobranza_liquidaciones",
        _id(), _tenant(),
        sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("periodo", sa.Text(), nullable=False, server_default="diario"),
        sa.Column("caja_anterior", _NUM, nullable=False, server_default="0"),
        sa.Column("caja_actual", _NUM, nullable=False, server_default="0"),
        sa.Column("total_cobrado", _NUM, nullable=False, server_default="0"),
        sa.Column("total_prestado", _NUM, nullable=False, server_default="0"),
        sa.Column("total_gastos", _NUM, nullable=False, server_default="0"),
        sa.Column("comision_valor", _NUM, nullable=False, server_default="0"),
        sa.Column("comentario", sa.Text(), nullable=True),
        *_audit(),
    )
    op.create_index("ix_pack_cobranza_liquidaciones_tenant_cartera", "pack_cobranza_liquidaciones", ["tenant_id", "cartera_id"])
    _rls("pack_cobranza_liquidaciones")


def downgrade() -> None:
    for table in (
        "pack_cobranza_liquidaciones",
        "pack_cobranza_reglas_negociacion",
        "pack_cobranza_promesas",
        "pack_cobranza_visitas",
        "pack_cobranza_abonos",
        "pack_cobranza_pagos",
        "pack_cobranza_cuotas",
        "pack_cobranza_prestamos",
        "pack_cobranza_clientes",
        "pack_cobranza_gasto_evidencias",
        "pack_cobranza_gastos",
        "pack_cobranza_inyecciones",
        "pack_cobranza_caja_ajustes",
        "pack_cobranza_cajas",
        "pack_cobranza_cartera_config",
        "pack_cobranza_carteras",
        "pack_cobranza_gestor_permisos",
        "pack_cobranza_gestores",
    ):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.drop_table(table)
