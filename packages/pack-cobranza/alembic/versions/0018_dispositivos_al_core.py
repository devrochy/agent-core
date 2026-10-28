"""Dispositivos y intentos de acceso del gestor → core (ADR-0174, SPEC-42, HU-ACC-02).

Copia los dispositivos vinculados a un gestor (`pack_cobranza_devices`) a `account_devices` del core
(por cuenta: `Gestor.admin_id`) y los intentos de acceso de los últimos 90 días
(`pack_cobranza_intentos_acceso`) a `access_events` (tipo `legado_pack`). Es una COPIA: las tablas del
pack no se tocan ni se borran (queda congelada; invariante 8). Idempotente: una segunda corrida no crea
filas nuevas (único parcial por `legacy_pack_device_id` y `detalle.intento_id` para los eventos).

Qué se migra y qué se omite (migrados + omitidos = origen, verificado por negocio):
- Se migran los dispositivos no borrados de un gestor no borrado con `admin_id` y cuenta existente. El
  estado se conserva tal cual (incluido `bloqueado`: perderlo reabriría el acceso al equipo).
- Un dispositivo sin `imei` (vínculo manual de ADR-0068) queda `legacy`: sin `device_id` hasta el primer
  login del gestor, que lo rellena. Nadie puede ser denegado por una fila así antes de eso.
- Se omiten (y se cuentan en el log) los dispositivos de un gestor sin `admin_id`, borrado o cuyo admin ya
  no existe. Los dispositivos sin gestor (v1: código + API key) no son parte del origen: siguen en el pack.

Corre con `app.tenant_id` fijado por negocio (RLS forzada en `account_devices` y `access_events`), igual que
0051 del core. Si falla, no aborta las migraciones de los demás packs (`upgrade_all`): el login adopta lo
que falte, de a un gestor, con `adoptar_legado` (red de seguridad).

Downgrade: borra las filas migradas de `account_devices` (los eventos `legado_pack` se quedan: la tabla es
append-only por diseño). Ojo: los bloqueos decididos en el core DESPUÉS de esta migración se pierden (la tabla
del pack quedó congelada con el estado anterior); bajar de aquí en producción exige revisar los equipos bloqueados.

Revision ID: pack_cobranza_0018
Revises: pack_cobranza_0017
Create Date: 2026-10-27
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "pack_cobranza_0018"
down_revision = "pack_cobranza_0017"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_VENTANA_DIAS = 90

# Dispositivos del pack que SÍ tienen cuenta a la que atarse.
_MIGRABLES = """
from pack_cobranza_devices d
join pack_cobranza_gestores g on g.id = d.gestor_id and g.tenant_id = d.tenant_id
join admins a on a.id = g.admin_id
where d.tenant_id = :tid and d.deleted_at is null and g.deleted_at is null and g.admin_id is not null
"""

_COPIAR_DISPOSITIVOS = f"""
insert into account_devices (
    id, tenant_id, admin_id, device_id, plataforma, estado, origen, rol_al_registrar, bloqueo_equipo,
    legacy, legacy_pack_device_id, aprobado_at, bloqueado_at, created_at, updated_at
)
select gen_random_uuid(), d.tenant_id, g.admin_id, nullif(btrim(d.imei), ''), 'android',
       case when d.estado in ('activo', 'bloqueado', 'pendiente') then d.estado else 'pendiente' end,
       'migrado_pack', 'gestor', false,
       nullif(btrim(d.imei), '') is null, d.id,
       case when d.estado = 'activo' then coalesce(d.fecha_vinculacion, d.created_at) end,
       case when d.estado = 'bloqueado' then now() end,
       d.created_at, now()
{_MIGRABLES}
on conflict do nothing
"""

_CONTAR_ORIGEN = """
select count(*) from pack_cobranza_devices d
where d.tenant_id = :tid and d.deleted_at is null and d.gestor_id is not null
"""

_CONTAR_MIGRADOS = f"""
select count(*) {_MIGRABLES}
and exists (
    select 1 from account_devices ad
    where ad.tenant_id = d.tenant_id and ad.legacy_pack_device_id = d.id
)
"""

_COPIAR_EVENTOS = f"""
insert into access_events (
    id, tenant_id, admin_id, device_id, rol, canal, tipo, severidad, ip, detalle, occurred_at
)
select gen_random_uuid(), i.tenant_id, g.admin_id, nullif(btrim(i.imei), ''), 'gestor', 'campo',
       'legado_pack', case when i.resultado = 'denegado' then 'aviso' else 'info' end, i.ip,
       jsonb_build_object('origen', 'pack_intentos', 'intento_id', i.id::text,
                          'resultado', i.resultado, 'motivo', i.motivo),
       i.created_at
from pack_cobranza_intentos_acceso i
left join pack_cobranza_gestores g on g.id = i.gestor_id and g.tenant_id = i.tenant_id
where i.tenant_id = :tid
  and i.created_at >= now() - interval '{_VENTANA_DIAS} days'
  and not exists (
      select 1 from access_events e
      where e.tenant_id = i.tenant_id and e.tipo = 'legado_pack'
        and e.detalle ->> 'intento_id' = i.id::text
  )
"""


def _fijar_tenant(conn: sa.engine.Connection, tenant_id: object) -> None:
    conn.execute(
        sa.text("select set_config('app.tenant_id', :tid, false)"), {"tid": str(tenant_id)}
    )


def upgrade() -> None:
    conn = op.get_bind()
    if conn.execute(sa.text("select to_regclass('account_devices')")).scalar_one() is None:
        raise RuntimeError(
            "pack_cobranza_0018 requiere la migración 0113 del core (account_devices)"
        )
    tenant_ids = [row[0] for row in conn.execute(sa.text("select id from tenants"))]
    for tenant_id in tenant_ids:
        _fijar_tenant(conn, tenant_id)
        params = {"tid": tenant_id}
        origen = conn.execute(sa.text(_CONTAR_ORIGEN), params).scalar_one()
        if (
            not origen
            and not conn.execute(
                sa.text(
                    "select 1 from pack_cobranza_intentos_acceso where tenant_id = :tid limit 1"
                ),
                params,
            ).first()
        ):
            continue
        conn.execute(sa.text(_COPIAR_DISPOSITIVOS), params)
        eventos = conn.execute(sa.text(_COPIAR_EVENTOS), params).rowcount
        migrados = conn.execute(sa.text(_CONTAR_MIGRADOS), params).scalar_one()
        omitidos = origen - migrados
        legacy = conn.execute(
            sa.text(
                "select count(*) from account_devices where tenant_id = :tid "
                "and origen = 'migrado_pack' and legacy"
            ),
            params,
        ).scalar_one()
        log.info(
            "pack_cobranza_0018 tenant=%s origen=%d migrados=%d omitidos=%d legacy=%d eventos=%d",
            tenant_id,
            origen,
            migrados,
            omitidos,
            legacy,
            eventos,
        )
        # Los omitidos son exactamente los que no tienen cuenta a la que atarse: si no, algo se perdió.
        sin_cuenta = conn.execute(
            sa.text(
                """
                select count(*) from pack_cobranza_devices d
                left join pack_cobranza_gestores g on g.id = d.gestor_id and g.tenant_id = d.tenant_id
                left join admins a on a.id = g.admin_id
                where d.tenant_id = :tid and d.deleted_at is null and d.gestor_id is not null
                  and (g.id is null or g.deleted_at is not null or g.admin_id is null or a.id is null)
                """
            ),
            params,
        ).scalar_one()
        if migrados + sin_cuenta != origen:
            raise RuntimeError(
                f"pack_cobranza_0018: tenant {tenant_id}: origen={origen}, migrados={migrados}, "
                f"omitidos esperados={sin_cuenta}; no suman"
            )
    conn.execute(sa.text("select set_config('app.tenant_id', '', false)"))


def downgrade() -> None:
    conn = op.get_bind()
    for (tenant_id,) in conn.execute(sa.text("select id from tenants")).all():
        _fijar_tenant(conn, tenant_id)
        conn.execute(
            sa.text(
                "delete from account_devices where tenant_id = :tid and origen = 'migrado_pack'"
            ),
            {"tid": tenant_id},
        )
    conn.execute(sa.text("select set_config('app.tenant_id', '', false)"))
