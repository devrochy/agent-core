"""Modelos de pack-cobranza — SQLAlchemy sobre su PROPIA Base (invariante nº5).

Alineados al modelo de dominio de `app-cobranza` **develop** (ADR-0060):
**Propietario → Cartera → Cliente → Préstamo**, con **Gestor de cartera** como
usuario interno. La entidad principal es **Cartera** ("ruta" es solo una feature,
`ruta_habilitada`).

Aislamiento multi-tenant (ADR-0059): **1 tenant = 1 propietario**. Toda tabla
lleva `tenant_id` (invariante nº1); las referencias al core (`customer_id`) son
FK **lógicas** (UUID sin constraint).

La geografía (`geom geography(Point,4326)`) se mantiene por un trigger definido
en la migración `0002_postgis.py` a partir de `lat`/`lng`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, Index, Integer, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.shared.tenant import get_current_tenant_id


def utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


def _tenant_or_raise() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("tenant_id requerido: usar dentro de tenant_scope()")
    return tenant_id


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=_tenant_or_raise
    )


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ identidad
# El PROPIETARIO es el tenant (ADR-0062): no hay entidad propia. Su config
# (moneda, país, oficina de cobro, días) vive en `CobranzaConfig`; sus permisos
# los resuelve el RBAC del core (owner → write, colaborador → read).


class PermisoMixin:
    """Pivot de permisos por usuario (permiso + habilitado)."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    permiso: Mapped[str] = mapped_column(Text, nullable=False)
    habilitado: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Gestor(TenantMixin, Base):
    """Perfil de gestor de cartera (usuario de campo).

    El gestor es un **colaborador del core** (ADR-0066): la identidad y las
    credenciales viven en `admins`/`tenant_user`; esta fila es el perfil del pack
    (cartera asignada, permisos de la app de campo, estatus de campo).
    """

    __tablename__ = "pack_cobranza_gestores"
    __table_args__ = (
        Index("ix_pack_cobranza_gestores_tenant_estatus", "tenant_id", "estatus"),
        Index("uq_pack_cobranza_gestor_admin", "tenant_id", "admin_id", unique=True, postgresql_where=text("admin_id is not null")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica a core.admins
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    apellido: Mapped[str | None] = mapped_column(Text, default=None)
    correo: Mapped[str | None] = mapped_column(Text, default=None)
    telefono: Mapped[str | None] = mapped_column(Text, default=None)
    codigo: Mapped[str | None] = mapped_column(Text, default=None)
    estatus: Mapped[str] = mapped_column(Text, default="activo")
    custom: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Device(TenantMixin, Base):
    """Dispositivo vinculado a un gestor (ADR-0057): código + API key hasheada."""

    __tablename__ = "pack_cobranza_devices"
    __table_args__ = (
        Index("uq_pack_cobranza_device_codigo", "tenant_id", "codigo", unique=True),
        Index("ix_pack_cobranza_devices_tenant_gestor", "tenant_id", "gestor_id"),
        Index("uq_pack_cobranza_device_imei", "tenant_id", "imei", unique=True, postgresql_where=text("imei is not null and deleted_at is null")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    gestor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica
    cartera_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    imei: Mapped[str | None] = mapped_column(Text, default=None)
    whatsapp_number: Mapped[str | None] = mapped_column(Text, default=None)
    public_key: Mapped[str | None] = mapped_column(Text, default=None)
    estado: Mapped[str] = mapped_column(Text, default="activo")  # activo|bloqueado
    fecha_vinculacion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntentoAcceso(TenantMixin, Base):
    """Auditoría de intentos de acceso de un dispositivo (IMEI/WhatsApp)."""

    __tablename__ = "pack_cobranza_intentos_acceso"
    __table_args__ = (Index("ix_pack_cobranza_intentos_acceso_tenant_gestor", "tenant_id", "gestor_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gestor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    imei: Mapped[str | None] = mapped_column(Text, default=None)
    whatsapp_number: Mapped[str | None] = mapped_column(Text, default=None)
    resultado: Mapped[str] = mapped_column(Text, nullable=False)  # ok|denegado
    motivo: Mapped[str | None] = mapped_column(Text, default=None)
    ip: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncEvento(TenantMixin, Base):
    """Evento offline sincronizado por un dispositivo (idempotente por evento de cliente)."""

    __tablename__ = "pack_cobranza_sync_eventos"
    __table_args__ = (
        Index("uq_pack_cobranza_sync_evento", "tenant_id", "evento_id_cliente", unique=True),
        Index("ix_pack_cobranza_sync_eventos_tenant_device", "tenant_id", "device_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    evento_id_cliente: Mapped[str] = mapped_column(Text, nullable=False)
    tipo: Mapped[str] = mapped_column(Text, nullable=False)  # visita|pago|abono
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    estado: Mapped[str] = mapped_column(Text, default="aplicado")  # aplicado|error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GestorPermiso(TenantMixin, PermisoMixin, Base):
    __tablename__ = "pack_cobranza_gestor_permisos"
    __table_args__ = (
        Index("uq_pack_cobranza_gestor_permiso", "tenant_id", "gestor_id", "permiso", unique=True),
    )

    gestor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


# ------------------------------------------------------------------ cartera
class Cartera(TenantMixin, Base):
    """Cartera de clientes/préstamos (entidad principal). 'Ruta' es una feature."""

    __tablename__ = "pack_cobranza_carteras"
    __table_args__ = (Index("ix_pack_cobranza_carteras_tenant_estatus", "tenant_id", "estatus"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gestor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(Text, default=None)
    tipo_interes: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=0)
    num_cuotas: Mapped[int] = mapped_column(Integer, default=1)
    moneda: Mapped[str] = mapped_column(Text, default="COP")
    costo_cobro: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    estatus: Mapped[str] = mapped_column(Text, default="activo")
    custom: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CarteraConfig(TenantMixin, Base):
    """Parámetros operativos por cartera (1:1). Los flags `*_apk` controlan la APK."""

    __tablename__ = "pack_cobranza_cartera_config"
    __table_args__ = (Index("uq_pack_cobranza_cartera_config", "tenant_id", "cartera_id", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    cuotas_minimas_prestamo: Mapped[int] = mapped_column(Integer, default=1)
    cuotas_atraso_umbral: Mapped[int] = mapped_column(Integer, default=0)
    manejo_cupo_activo: Mapped[bool] = mapped_column(Boolean, default=False)
    cupo_default: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    recargo_activo: Mapped[bool] = mapped_column(Boolean, default=False)
    bloquear_cambio_interes: Mapped[bool] = mapped_column(Boolean, default=False)
    comision_activa: Mapped[bool] = mapped_column(Boolean, default=False)
    comision_porcentaje: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    mostrar_fecha_ultima_liquidada: Mapped[bool] = mapped_column(Boolean, default=True)
    mostrar_caja: Mapped[bool] = mapped_column(Boolean, default=True)
    mostrar_cobrado_liquidada: Mapped[bool] = mapped_column(Boolean, default=True)
    mostrar_prestamos: Mapped[bool] = mapped_column(Boolean, default=True)
    mostrar_cobro_estimado: Mapped[bool] = mapped_column(Boolean, default=True)
    ocultar_cartera: Mapped[bool] = mapped_column(Boolean, default=False)
    eliminar_prestamos_apk: Mapped[bool] = mapped_column(Boolean, default=False)
    eliminar_pagos_apk: Mapped[bool] = mapped_column(Boolean, default=False)
    eliminar_abonos_apk: Mapped[bool] = mapped_column(Boolean, default=False)
    eliminar_gastos_apk: Mapped[bool] = mapped_column(Boolean, default=False)
    eliminar_inyeccion_apk: Mapped[bool] = mapped_column(Boolean, default=False)
    registrar_inyeccion_apk: Mapped[bool] = mapped_column(Boolean, default=False)
    generar_reportes_apk: Mapped[bool] = mapped_column(Boolean, default=False)
    reconocimiento_facial_activo: Mapped[bool] = mapped_column(Boolean, default=False)
    registro_documento_cliente: Mapped[bool] = mapped_column(Boolean, default=False)
    bloqueo_automatico_clientes: Mapped[bool] = mapped_column(Boolean, default=False)
    borrar_clientes_sin_deuda: Mapped[bool] = mapped_column(Boolean, default=False)
    permitir_cambio_fecha_prestamo: Mapped[bool] = mapped_column(Boolean, default=False)
    dias_no_laborables: Mapped[str] = mapped_column(Text, default="solo_domingos")
    periodo_liquidacion: Mapped[str] = mapped_column(Text, default="diario")
    dias_anticipacion_notificacion: Mapped[int] = mapped_column(Integer, default=0)
    aviso_dia_cobro: Mapped[bool] = mapped_column(Boolean, default=False)
    umbral_mora_notificacion: Mapped[int] = mapped_column(Integer, default=0)
    ruta_habilitada: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Caja(TenantMixin, Base):
    """Caja de la cartera (1:1)."""

    __tablename__ = "pack_cobranza_cajas"
    __table_args__ = (Index("uq_pack_cobranza_caja", "tenant_id", "cartera_id", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    saldo_inicial: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    saldo_actual: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CajaAjuste(TenantMixin, Base):
    __tablename__ = "pack_cobranza_caja_ajustes"
    __table_args__ = (Index("ix_pack_cobranza_caja_ajustes_tenant_caja", "tenant_id", "caja_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caja_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    valor_anterior: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    valor_nuevo: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    motivo: Mapped[str] = mapped_column(Text, nullable=False)
    actor_rol: Mapped[str | None] = mapped_column(Text, default=None)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Inyeccion(TenantMixin, Base):
    """Aporte de capital a la caja de una cartera."""

    __tablename__ = "pack_cobranza_inyecciones"
    __table_args__ = (Index("ix_pack_cobranza_inyecciones_tenant_cartera", "tenant_id", "cartera_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    comentario: Mapped[str | None] = mapped_column(Text, default=None)
    estado: Mapped[str] = mapped_column(Text, default="activa")  # activa|eliminada
    fecha_hora: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Gasto(TenantMixin, Base):
    """Gasto operativo de una cartera, con flujo de aprobación y evidencias."""

    __tablename__ = "pack_cobranza_gastos"
    __table_args__ = (Index("ix_pack_cobranza_gastos_tenant_cartera", "tenant_id", "cartera_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    creado_por: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    aprobado: Mapped[bool] = mapped_column(Boolean, default=False)
    aprobado_por: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    estado: Mapped[str] = mapped_column(Text, default="activo")  # activo|eliminado
    fecha_hora: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GastoEvidencia(TenantMixin, Base):
    __tablename__ = "pack_cobranza_gasto_evidencias"
    __table_args__ = (Index("ix_pack_cobranza_gasto_evidencias_tenant_gasto", "tenant_id", "gasto_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gasto_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    ruta_archivo: Mapped[str] = mapped_column(Text, nullable=False)
    nombre_original: Mapped[str | None] = mapped_column(Text, default=None)
    mimetype: Mapped[str | None] = mapped_column(Text, default=None)
    tamano: Mapped[int | None] = mapped_column(Integer, default=None)
    creado_por_rol: Mapped[str | None] = mapped_column(Text, default=None)
    creado_por_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ cartera: clientes
class Cliente(TenantMixin, Base):
    """Cliente de cartera. `customer_id` es FK lógica al `Customer` del core."""

    __tablename__ = "pack_cobranza_clientes"
    __table_args__ = (
        Index("ix_pack_cobranza_clientes_tenant_cartera", "tenant_id", "cartera_id"),
        Index("ix_pack_cobranza_clientes_tenant_customer", "tenant_id", "customer_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    apellido: Mapped[str | None] = mapped_column(Text, default=None)
    negocio: Mapped[str | None] = mapped_column(Text, default=None)
    telefono_whatsapp: Mapped[str | None] = mapped_column(Text, default=None)
    tipo_documento: Mapped[str | None] = mapped_column(Text, default=None)
    numero_documento: Mapped[str | None] = mapped_column(Text, default=None)
    lat: Mapped[float | None] = mapped_column(Float, default=None)
    lng: Mapped[float | None] = mapped_column(Float, default=None)
    lat_domicilio: Mapped[float | None] = mapped_column(Float, default=None)
    lng_domicilio: Mapped[float | None] = mapped_column(Float, default=None)
    tope_maximo_deuda: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    color_riesgo: Mapped[str] = mapped_column(Text, default="blanco")  # blanco|azul|rojo
    estatus: Mapped[str] = mapped_column(Text, default="activo")
    custom: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Prestamo(TenantMixin, Base):
    __tablename__ = "pack_cobranza_prestamos"
    __table_args__ = (Index("ix_pack_cobranza_prestamos_tenant_cliente", "tenant_id", "cliente_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cliente_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    saldo: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    num_cuotas: Mapped[int] = mapped_column(Integer, default=1)
    tipo_interes: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=0)
    dias_entre_cuotas: Mapped[int] = mapped_column(Integer, default=1)
    fecha_otorgado: Mapped[date] = mapped_column(Date, nullable=False)
    fiador_nombre: Mapped[str | None] = mapped_column(Text, default=None)
    fiador_apellido: Mapped[str | None] = mapped_column(Text, default=None)
    fiador_documento: Mapped[str | None] = mapped_column(Text, default=None)
    fiador_telefono: Mapped[str | None] = mapped_column(Text, default=None)
    estatus: Mapped[str] = mapped_column(Text, default="vigente")  # vigente|liquidado|cancelado
    custom: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Cuota(TenantMixin, Base):
    __tablename__ = "pack_cobranza_cuotas"
    __table_args__ = (Index("ix_pack_cobranza_cuotas_tenant_prestamo", "tenant_id", "prestamo_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prestamo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    numero_cuota: Mapped[int] = mapped_column(Integer, nullable=False)
    valor_esperado: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    fecha_vencimiento: Mapped[date] = mapped_column(Date, nullable=False)
    estatus: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|pagada|atrasada
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Pago(TenantMixin, Base):
    """Pago de una cuota (o pago huérfano, sin cuota)."""

    __tablename__ = "pack_cobranza_pagos"
    __table_args__ = (
        Index("ix_pack_cobranza_pagos_tenant_cliente", "tenant_id", "cliente_id"),
        Index("uq_pack_cobranza_pagos_tenant_idem", "tenant_id", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cliente_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    prestamo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    cuota_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    metodo_pago: Mapped[str] = mapped_column(Text, default="efectivo")
    registrado_por: Mapped[str | None] = mapped_column(Text, default=None)  # gestor|propietario|agente|ia
    idempotency_key: Mapped[str | None] = mapped_column(Text, default=None)
    fecha_hora: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Abono(TenantMixin, Base):
    """Abono parcial a un préstamo (no cubre una cuota completa)."""

    __tablename__ = "pack_cobranza_abonos"
    __table_args__ = (
        Index("ix_pack_cobranza_abonos_tenant_cliente", "tenant_id", "cliente_id"),
        Index("uq_pack_cobranza_abonos_tenant_idem", "tenant_id", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prestamo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    cliente_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    visita_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    metodo_pago: Mapped[str] = mapped_column(Text, default="efectivo")
    registrado_por: Mapped[str | None] = mapped_column(Text, default=None)
    idempotency_key: Mapped[str | None] = mapped_column(Text, default=None)
    fecha_hora: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Visita(TenantMixin, Base):
    __tablename__ = "pack_cobranza_visitas"
    __table_args__ = (Index("ix_pack_cobranza_visitas_tenant_cartera", "tenant_id", "cartera_id", "fecha"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    cliente_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    resultado: Mapped[str] = mapped_column(Text, nullable=False)  # pago|no_pago
    motivo_no_pago: Mapped[str | None] = mapped_column(Text, default=None)
    valor_pagado: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    metodo_pago: Mapped[str | None] = mapped_column(Text, default=None)
    creado_por: Mapped[str | None] = mapped_column(Text, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Promesa(TenantMixin, Base):
    __tablename__ = "pack_cobranza_promesas"
    __table_args__ = (
        Index("ix_pack_cobranza_promesas_tenant_prestamo", "tenant_id", "prestamo_id"),
        Index("uq_pack_cobranza_promesas_tenant_idem", "tenant_id", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prestamo_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    cliente_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fecha_prometida: Mapped[date] = mapped_column(Date, nullable=False)
    valor_prometido: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    estado: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|cumplida|incumplida
    tipo: Mapped[str] = mapped_column(Text, default="promesa")  # promesa|abono_parcial|refinanciacion
    creado_por: Mapped[str] = mapped_column(Text, default="ia")  # ia|gestor|propietario|agente
    idempotency_key: Mapped[str | None] = mapped_column(Text, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReglaNegociacion(TenantMixin, Base):
    """Reglas deterministas de negociación del asistente. Una fila por tenant."""

    __tablename__ = "pack_cobranza_reglas_negociacion"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    max_dias_prorroga: Mapped[int] = mapped_column(Integer, default=15)
    min_abono_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=10)
    max_reprogramaciones: Mapped[int] = mapped_column(Integer, default=2)
    umbral_saldo_autonomo: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditoriaCartera(TenantMixin, Base):
    """Auditoría imborrable de operaciones sobre cuotas/abonos/pagos/promesas."""

    __tablename__ = "pack_cobranza_auditoria"
    __table_args__ = (Index("ix_pack_cobranza_auditoria_tenant_entidad", "tenant_id", "entidad", "entidad_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entidad: Mapped[str] = mapped_column(Text, nullable=False)  # cuota|abono|pago|promesa|prestamo
    entidad_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operacion: Mapped[str] = mapped_column(Text, nullable=False)  # crear|editar|eliminar
    valores_antes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    valores_despues: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    actor_rol: Mapped[str | None] = mapped_column(Text, default=None)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    motivo: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CambioCliente(TenantMixin, Base):
    """Propuesta de cambio de datos de un cliente, con aprobación (HU-47)."""

    __tablename__ = "pack_cobranza_cambios_cliente"
    __table_args__ = (Index("ix_pack_cobranza_cambios_cliente_tenant_cliente", "tenant_id", "cliente_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cliente_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    campos_propuestos: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    estado: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|aprobado|rechazado
    solicitado_por_rol: Mapped[str | None] = mapped_column(Text, default=None)
    solicitado_por_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    revisado_por: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    revisado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    motivo_rechazo: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClienteEvidencia(TenantMixin, Base):
    """Foto facial o de documento del cliente (según flags de la cartera)."""

    __tablename__ = "pack_cobranza_cliente_evidencias"
    __table_args__ = (Index("ix_pack_cobranza_cliente_evidencias_tenant_cliente", "tenant_id", "cliente_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cliente_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    tipo: Mapped[str] = mapped_column(Text, nullable=False)  # foto_facial|documento_frente|documento_reverso
    ruta_archivo: Mapped[str] = mapped_column(Text, nullable=False)
    nombre_original: Mapped[str | None] = mapped_column(Text, default=None)
    mimetype: Mapped[str | None] = mapped_column(Text, default=None)
    tamano: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CarteraNota(TenantMixin, Base):
    """Nota libre sobre una cartera (observaciones del circuito)."""

    __tablename__ = "pack_cobranza_cartera_notas"
    __table_args__ = (Index("ix_pack_cobranza_cartera_notas_tenant_cartera", "tenant_id", "cartera_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    nota: Mapped[str] = mapped_column(Text, nullable=False)
    creado_por_rol: Mapped[str | None] = mapped_column(Text, default=None)
    creado_por_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CarteraApertura(TenantMixin, Base):
    """Apertura de la cartera del día (inicio de jornada + ubicación)."""

    __tablename__ = "pack_cobranza_carteras_aperturas"
    __table_args__ = (Index("ix_pack_cobranza_carteras_aperturas_tenant_cartera", "tenant_id", "cartera_id", "fecha"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    hora_inicio: Mapped[str | None] = mapped_column(Text, default=None)
    lat: Mapped[float | None] = mapped_column(Float, default=None)
    lng: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PosicionGestor(TenantMixin, Base):
    """Posición reportada por el gestor (tracking de campo, v2)."""

    __tablename__ = "pack_cobranza_posicion_gestor"
    __table_args__ = (Index("ix_pack_cobranza_posicion_gestor_tenant_cartera", "tenant_id", "cartera_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gestor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    registrada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReporteDiario(TenantMixin, Base):
    """Snapshot del reporte diario de una cartera."""

    __tablename__ = "pack_cobranza_reportes_diarios"
    __table_args__ = (Index("ix_pack_cobranza_reportes_diarios_tenant_cartera", "tenant_id", "cartera_id", "fecha"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    cobrado: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    prestado: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    clientes_visitados: Mapped[list[Any] | None] = mapped_column(JSONB, default=None)
    clientes_sin_pago: Mapped[list[Any] | None] = mapped_column(JSONB, default=None)
    trayectorias: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    hora_inicio: Mapped[str | None] = mapped_column(Text, default=None)
    hora_fin: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CarteraEstadisticasSnapshot(TenantMixin, Base):
    """KPIs de la cartera congelados a una fecha (para delta vs. ayer)."""

    __tablename__ = "pack_cobranza_cartera_estadisticas"
    __table_args__ = (Index("ix_pack_cobranza_cartera_estadisticas_tenant_cartera", "tenant_id", "cartera_id", "fecha"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    datos: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CarteraOptimizadaLog(TenantMixin, Base):
    """Log de la optimización del trayecto diario (orden de clientes)."""

    __tablename__ = "pack_cobranza_cartera_optimizada"
    __table_args__ = (Index("ix_pack_cobranza_cartera_optimizada_tenant_cartera", "tenant_id", "cartera_id", "fecha"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    orden_clientes: Mapped[list[Any] | None] = mapped_column(JSONB, default=None)
    distancia_estimada_km: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)
    tiempo_estimado_min: Mapped[int | None] = mapped_column(Integer, default=None)
    recalculado: Mapped[bool] = mapped_column(Boolean, default=False)
    motivo_recalculo: Mapped[str | None] = mapped_column(Text, default=None)
    tipo: Mapped[str] = mapped_column(Text, default="planificada")  # planificada|real
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CobroPropietario(TenantMixin, Base):
    """Cobro periódico al propietario por el servicio (Épica 9).

    Como el propietario ES el tenant (ADR-0062), el cobro es a nivel de tenant.
    """

    __tablename__ = "pack_cobranza_cobros_propietario"
    __table_args__ = (Index("uq_pack_cobranza_cobro_periodo", "tenant_id", "periodo", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    periodo: Mapped[str] = mapped_column(Text, nullable=False)  # YYYY-MM
    monto_calculado: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    monto_pagado: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    fecha_vencimiento: Mapped[date] = mapped_column(Date, nullable=False)
    fecha_pago: Mapped[date | None] = mapped_column(Date, default=None)
    estado: Mapped[str] = mapped_column(Text, default="pendiente")  # pendiente|pagado|vencido
    metodo_pago: Mapped[str | None] = mapped_column(Text, default=None)
    registrado_por: Mapped[str | None] = mapped_column(Text, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LinkPago(TenantMixin, Base):
    """Link de pago asociado a un cobro al propietario (Épica 9)."""

    __tablename__ = "pack_cobranza_links_pago"
    __table_args__ = (
        Index("ix_pack_cobranza_links_pago_tenant_cobro", "tenant_id", "cobro_id"),
        Index("uq_pack_cobranza_links_pago_referencia", "tenant_id", "referencia", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cobro_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica
    # Cobro digital (ADR-0067): un link puede apuntar a una cuota de un cliente.
    cuota_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica
    prestamo_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica
    cliente_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica
    referencia: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    monto: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    estado: Mapped[str] = mapped_column(Text, default="activo")  # activo|usado|expirado
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversacionPropietario(TenantMixin, Base):
    """Hilo de comunicación con el propietario (Épica 9, HU-63).

    El propietario ES el tenant (ADR-0062), así que hay un hilo por tenant.
    """

    __tablename__ = "pack_cobranza_conversaciones_propietario"
    __table_args__ = (Index("uq_pack_cobranza_conversacion_propietario", "tenant_id", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asunto: Mapped[str] = mapped_column(Text, default="Comunicación con el propietario")
    estado: Mapped[str] = mapped_column(Text, default="abierta")  # abierta|archivada
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MensajePropietario(TenantMixin, Base):
    """Mensaje del hilo con el propietario: notificación de cobro o mensaje manual."""

    __tablename__ = "pack_cobranza_mensajes_propietario"
    __table_args__ = (Index("ix_pack_cobranza_mensajes_propietario_tenant_conversacion", "tenant_id", "conversacion_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversacion_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # FK lógica
    direccion: Mapped[str] = mapped_column(Text, default="saliente")  # saliente|entrante
    tipo: Mapped[str] = mapped_column(Text, default="manual")  # notificacion|manual
    autor_rol: Mapped[str | None] = mapped_column(Text, default=None)  # admin|propietario|sistema
    contenido: Mapped[str] = mapped_column(Text, nullable=False)
    cobro_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), default=None)  # FK lógica
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Liquidacion(TenantMixin, Base):
    __tablename__ = "pack_cobranza_liquidaciones"
    __table_args__ = (Index("ix_pack_cobranza_liquidaciones_tenant_cartera", "tenant_id", "cartera_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cartera_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    periodo: Mapped[str] = mapped_column(Text, default="diario")
    caja_anterior: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    caja_actual: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    total_cobrado: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    total_prestado: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    total_gastos: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    comision_valor: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    # KPIs del día (REQ-APK-003): calculados al generar la liquidación.
    estimado_acobrar: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    total_inyeccion: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    suma_cartera: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    comision_porcentaje: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=0)
    detalle: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    comentario: Mapped[str | None] = mapped_column(Text, default=None)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
