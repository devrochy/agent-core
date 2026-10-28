"""Dispositivos de la cuenta y eventos de acceso en el core (ADR-0174, SPEC-42, HU-ACC-01).

Dos tablas de NEGOCIO: `tenant_id` + RLS habilitada y forzada + policy de aislamiento (invariante 1) e índices
compuestos que empiezan por `tenant_id`.

- `account_devices`: «esta cuenta, en este equipo, en este negocio». La clave es la cuenta (`admin_id`), no el
  perfil de gestor del pack: así el 500 por equipo compartido desaparece de raíz (el único es por cuenta). CHECK
  de estado/origen/plataforma, `device_id` obligatorio salvo filas heredadas del pack (`legacy`), únicos
  parciales (idempotencia del alta y de la migración del pack) y baja lógica (`deleted_at`, invariante 8).
- `access_events`: registro de eventos de acceso, append-only: el trigger `prevent_mutation('deleted_at')` (0067)
  rechaza DELETE, TRUNCATE y todo UPDATE salvo la baja lógica por retención. `app_runtime` no recibe DELETE.

También copia las concesiones de permiso existentes de `pack_cobranza:dispositivos` → `devices` y de
`pack_cobranza:alertas` → `access_alerts` (tabla de sistema `admin_permissions`), para que nadie pierda el
acceso. Es idempotente (no duplica una concesión ya copiada).

Alcance de la copia: `devices` cubre los equipos de TODAS las cuentas del negocio (el permiso del pack solo
cubría los de gestores) y una concesión global (`tenant_id` null) vale ahora también en negocios sin el pack.
Es lo que decide ADR-0174 («nadie pierde el acceso»); los colaboradores sin concesión explícita no heredan
nada (`devices` y `access_alerts` son opt-in).

Aditiva y reversible: tablas nuevas y vacías, sin reescritura ni bloqueo largo. El downgrade elimina TODAS las
filas de `devices` y `access_alerts` de `admin_permissions` (también las concedidas después de esta migración,
porque la tabla no distingue las copiadas) y las tablas en orden inverso (se pierden los equipos y eventos
registrados; los datos del pack siguen intactos porque esta migración no los toca).

Revision ID: 0113
Revises: 0112
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_AHORA = sa.text("now()")

_EVENTOS = (
    "login_fallido",
    "login_bloqueado",
    "dispositivo_nuevo",
    "dispositivo_pendiente",
    "dispositivo_aprobado",
    "dispositivo_bloqueado_usado",
    "acceso_denegado_politica",
    "dispositivo_sin_id",
    "dispositivo_compartido",
    "politica_cambiada",
    "acceso_master",
    "legado_pack",
)
_COPIA_PERMISOS = {
    "pack_cobranza:dispositivos": "devices",
    "pack_cobranza:alertas": "access_alerts",
}


def _en(valores: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in valores)


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    # Sin DELETE: la baja es lógica (UPDATE de `deleted_at`). Los default privileges de 0003 le dan DELETE al
    # rol de la app, así que se revoca antes de conceder (como 0069 y 0112). Solo `purge_tenant` borra.
    op.execute(f"revoke all on {table} from app_runtime")
    op.execute(f"grant select, insert, update on {table} to app_runtime")


def upgrade() -> None:
    op.create_table(
        "account_devices",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("admin_id", _UUID, sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=True),
        sa.Column("device_label", sa.Text(), nullable=True),
        sa.Column("plataforma", sa.Text(), nullable=False, server_default="android"),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("origen", sa.Text(), nullable=False, server_default="autoregistro"),
        sa.Column("rol_al_registrar", sa.Text(), nullable=True),
        sa.Column("bloqueo_equipo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legacy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("legacy_pack_device_id", _UUID, nullable=True),
        sa.Column("ultimo_uso_at", _TS, nullable=True),
        sa.Column("ultima_ip", sa.Text(), nullable=True),
        sa.Column("aprobado_por", _UUID, nullable=True),
        sa.Column("aprobado_at", _TS, nullable=True),
        sa.Column("bloqueado_por", _UUID, nullable=True),
        sa.Column("bloqueado_at", _TS, nullable=True),
        sa.Column("motivo", sa.Text(), nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        sa.CheckConstraint(
            "plataforma in ('android', 'ios')", name="ck_account_devices_plataforma"
        ),
        sa.CheckConstraint(
            "estado in ('pendiente', 'activo', 'bloqueado')", name="ck_account_devices_estado"
        ),
        sa.CheckConstraint(
            "origen in ('autoregistro', 'politica_abierta', 'alta_manual', 'migrado_pack')",
            name="ck_account_devices_origen",
        ),
        # Solo una fila heredada del pack puede no tener equipo hasta su primer login.
        sa.CheckConstraint("device_id is not null or legacy", name="ck_account_devices_equipo"),
    )
    op.create_index(
        "uq_account_devices_cuenta_equipo",
        "account_devices",
        ["tenant_id", "admin_id", "device_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null and device_id is not null"),
    )
    op.create_index(
        "uq_account_devices_legacy_pack",
        "account_devices",
        ["tenant_id", "legacy_pack_device_id"],
        unique=True,
        postgresql_where=sa.text("legacy_pack_device_id is not null"),
    )
    op.create_index("ix_account_devices_estado", "account_devices", ["tenant_id", "estado"])
    op.create_index("ix_account_devices_equipo", "account_devices", ["tenant_id", "device_id"])
    op.create_index("ix_account_devices_cuenta", "account_devices", ["tenant_id", "admin_id"])
    _rls("account_devices")

    op.create_table(
        "access_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("admin_id", _UUID, nullable=True),
        sa.Column("device_row_id", _UUID, nullable=True),
        sa.Column("device_id", sa.Text(), nullable=True),
        sa.Column("rol", sa.Text(), nullable=True),
        sa.Column("canal", sa.Text(), nullable=False, server_default="campo"),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("severidad", sa.Text(), nullable=False, server_default="info"),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("detalle", JSONB, nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("occurred_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.CheckConstraint(f"tipo in ({_en(_EVENTOS)})", name="ck_access_events_tipo"),
        sa.CheckConstraint(
            "severidad in ('info', 'aviso', 'critico')", name="ck_access_events_severidad"
        ),
        sa.CheckConstraint(
            "canal in ('campo', 'panel_apk', 'campo_v1')", name="ck_access_events_canal"
        ),
    )
    op.create_index(
        "ix_access_events_feed",
        "access_events",
        ["tenant_id", sa.text("occurred_at desc"), sa.text("id desc")],
    )
    op.create_index(
        "ix_access_events_severidad",
        "access_events",
        ["tenant_id", "severidad", sa.text("occurred_at desc")],
    )
    op.create_index(
        "ix_access_events_cuenta",
        "access_events",
        ["tenant_id", "admin_id", sa.text("occurred_at desc")],
    )
    _rls("access_events")
    # Append-only también a nivel de privilegios: la app solo puede dar de baja (retención), no editar eventos.
    op.execute("revoke update on access_events from app_runtime")
    op.execute("grant update (deleted_at) on access_events to app_runtime")
    # Append-only: solo se permite la baja lógica por retención (función genérica de 0067).
    op.execute(
        "CREATE TRIGGER access_events_inmutable BEFORE UPDATE OR DELETE ON access_events "
        "FOR EACH ROW EXECUTE FUNCTION prevent_mutation('deleted_at')"
    )
    op.execute(
        "CREATE TRIGGER access_events_sin_vaciar BEFORE TRUNCATE ON access_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation()"
    )

    # Concesiones existentes del pack → secciones del core (conservar el acceso equivalente). Tabla de sistema.
    for origen, destino in _COPIA_PERMISOS.items():
        op.execute(
            f"""
            insert into admin_permissions (id, admin_id, section, access, tenant_id, created_at)
            select gen_random_uuid(), p.admin_id, '{destino}', p.access, p.tenant_id, now()
            from admin_permissions p
            where p.section = '{origen}'
              and not exists (
                  select 1 from admin_permissions d
                  where d.admin_id = p.admin_id
                    and d.section = '{destino}'
                    and coalesce(d.tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)
                        = coalesce(p.tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)
              )
            """
        )


def downgrade() -> None:
    destinos = ", ".join(f"'{d}'" for d in _COPIA_PERMISOS.values())
    op.execute(f"delete from admin_permissions where section in ({destinos})")
    op.execute("drop trigger if exists access_events_sin_vaciar on access_events")
    op.execute("drop trigger if exists access_events_inmutable on access_events")
    for tabla in ("access_events", "account_devices"):
        op.execute(f"drop policy if exists tenant_isolation on {tabla}")
        op.execute(f"alter table {tabla} no force row level security")
        op.execute(f"alter table {tabla} disable row level security")
    op.drop_table("access_events")
    op.drop_table("account_devices")
