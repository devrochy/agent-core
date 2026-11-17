"""Copia de los leads de commerce a las tablas del core (ADR-0193 §7, HU-AOS-70).

`agent-core db copiar-leads`: por negocio y por lotes, copia `pack_commerce_leads`, `pack_commerce_lead_stage_changes`
y `pack_commerce_lead_followups` a `leads`, `lead_stage_changes` y `lead_followups` conservando ids, `customer_id`,
historia y seguimiento, y la config de intake y seguimiento de commerce a `lead_settings`. Los originales no se tocan.

Es una FOTO: lo ya copiado (por id) no se vuelve a copiar, así que correrla dos veces sobre los mismos datos da el
mismo resultado, pero un lead de commerce que cambió después de copiarse no se actualiza. El reporte cuenta esos
leads desactualizados; la copia definitiva se corre con commerce sin escrituras (al desplegar el cambio de dueño).

Un cliente, un lead vivo (único parcial `uq_leads_tenant_cliente`). Commerce permitía varios: entre los de un mismo
cliente queda vivo el de actividad más reciente y los demás se copian con `deleted_at` (sin borrado físico,
invariante 8; su historia sigue apuntando a ellos). Si el core ya tiene un lead vivo para ese cliente (lo creó el
core después del cambio de dueño), ese gana y el de commerce entra borrado. El reporte lista todos los que quedan
ocultos, con su etapa y si tenían un seguimiento vigente, para revisarlos antes de copiar (`--dry-run`).

Los leads copiados quedan con `origen` NULL: commerce no lo guardaba (NULL = lead anterior a ADR-0193).

Sin tablas de commerce (producción nace con base nueva, ❓4 de HU-AOS-70) no hace nada.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, text

from core.db.models import PluginConfig, Tenant
from core.db.session import get_session
from core.leads import config as config_leads
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

LOTE_POR_DEFECTO = 500
_COMMERCE = "pack_commerce"
# Campos de la config de commerce que pasan a Captar leads (lo demás de `CRMConfig` sigue en commerce o desaparece).
_CAMPOS_DE_CONFIG = tuple(config_leads.ConfigCaptarLeads.model_fields)

_COLUMNAS_LEAD = (
    "id, tenant_id, customer_id, conversation_id, name, phone, email, interest, score, status, idempotency_key, "
    "custom, last_activity_at, stage_changed_at, next_action_at, next_action_note, loss_reason, loss_note, "
    "estimated_value, created_at"
)

# Ranking de los leads de commerce de un cliente: 1 = el que queda vivo. Los leads sin cliente no compiten. El filtro
# explícito por negocio no depende solo de la RLS (defensa en profundidad si el rol de la CLI la saltara).
_RANKING = """
    select c.*, case when c.customer_id is null then 1 else row_number() over (
        partition by c.tenant_id, c.customer_id
        order by c.last_activity_at desc nulls last, c.created_at desc nulls last, c.id desc
    ) end as rango
    from pack_commerce_leads c
    where c.tenant_id = :tenant_id
"""

# Un lead de commerce que, vivo, chocaría con uno vivo del core: mismo cliente o misma clave de idempotencia.
_CHOCA_CON_EL_CORE = """
    exists (select 1 from leads l where l.deleted_at is null and l.tenant_id = r.tenant_id and (
        (r.customer_id is not null and l.customer_id = r.customer_id)
        or (r.idempotency_key is not null and l.idempotency_key = r.idempotency_key)))
"""

# Por lotes con keyset (`id > :ultimo`): el avance no depende de cuántas filas se insertaron, así que un lote que
# choca entero no corta la copia. El lote se elige en `_IDS_*` y se inserta con `id = any(:ids)`.
_IDS = {
    "leads": "select id from pack_commerce_leads where tenant_id = :tenant_id and id > :ultimo order by id limit :lote",
    "cambios": "select id from pack_commerce_lead_stage_changes where tenant_id = :tenant_id and id > :ultimo "
    "order by id limit :lote",
    "seguimientos": "select id from pack_commerce_lead_followups where tenant_id = :tenant_id and id > :ultimo "
    "order by id limit :lote",
}

# Vivo si es el más reciente de su cliente y no choca con un vivo del core; si no, entra borrado. ON CONFLICT DO
# NOTHING queda como red ante una carrera con un escritor del core (la fila quedaría sin copiar y en el reporte).
_COPIAR_LEADS = f"""
    with r as ({_RANKING})
    insert into leads ({_COLUMNAS_LEAD}, updated_at, deleted_at)
    select {_COLUMNAS_LEAD}, coalesce(r.last_activity_at, r.created_at),
           case when r.rango = 1 and not {_CHOCA_CON_EL_CORE} then null else now() end
    from r
    where r.id = any(:ids) and not exists (select 1 from leads l where l.id = r.id)
    on conflict do nothing
"""

_COPIAR_CAMBIOS = """
    insert into lead_stage_changes (id, tenant_id, lead_id, customer_id, from_stage, to_stage, actor,
                                    idempotency_key, changed_at, loss_reason, loss_note)
    select id, tenant_id, lead_id, customer_id, from_stage, to_stage, actor, idempotency_key, changed_at,
           loss_reason, loss_note
    from pack_commerce_lead_stage_changes c
    where c.tenant_id = :tenant_id and c.id = any(:ids)
      and not exists (select 1 from lead_stage_changes l where l.id = c.id)
    on conflict do nothing
"""

_COPIAR_SEGUIMIENTOS = """
    insert into lead_followups (id, tenant_id, lead_id, customer_id, note, due_at, outcome, actor, closed_at)
    select id, tenant_id, lead_id, customer_id, note, due_at, outcome, actor, closed_at
    from pack_commerce_lead_followups c
    where c.tenant_id = :tenant_id and c.id = any(:ids)
      and not exists (select 1 from lead_followups l where l.id = c.id)
    on conflict do nothing
"""

_PENDIENTES = {
    "leads": "select count(*) from pack_commerce_leads c where c.tenant_id = :tenant_id "
    "and not exists (select 1 from leads l where l.id = c.id)",
    "cambios": "select count(*) from pack_commerce_lead_stage_changes c where c.tenant_id = :tenant_id "
    "and not exists (select 1 from lead_stage_changes l where l.id = c.id)",
    "seguimientos": "select count(*) from pack_commerce_lead_followups c where c.tenant_id = :tenant_id "
    "and not exists (select 1 from lead_followups l where l.id = c.id)",
}

# Copiados cuyo original cambió después (la copia es una foto).
_DESACTUALIZADOS = """
    select count(*) from pack_commerce_leads c join leads l on l.id = c.id
    where c.tenant_id = :tenant_id and (
        c.last_activity_at is distinct from l.last_activity_at or c.status is distinct from l.status
        or c.next_action_at is distinct from l.next_action_at or c.loss_reason is distinct from l.loss_reason)
"""

# Los que quedarían ocultos al copiarlos: no son el más reciente de su cliente, o el core ya tiene uno vivo.
_OCULTOS_A_REVISAR = f"""
    with r as ({_RANKING})
    select r.id, r.customer_id, r.status, r.next_action_at
    from r
    where not exists (select 1 from leads l where l.id = r.id)
      and r.customer_id is not null
      and (r.rango > 1 or {_CHOCA_CON_EL_CORE})
    order by r.customer_id, r.id
"""


@dataclass(frozen=True)
class LeadOculto:
    tenant_id: uuid.UUID
    lead_id: uuid.UUID
    customer_id: uuid.UUID
    etapa: str
    seguimiento_vigente: bool


@dataclass
class ReporteCopia:
    dry_run: bool
    sin_tablas_de_commerce: bool = False
    negocios: int = 0
    leads: int = 0
    cambios: int = 0
    seguimientos: int = 0
    configs: int = 0
    desactualizados: int = 0
    ocultos: list[LeadOculto] = field(default_factory=list)
    configs_ilegibles: list[uuid.UUID] = field(default_factory=list)


async def _hay_tablas_de_commerce() -> bool:
    async with get_session() as session:
        return bool(
            (await session.execute(text("select to_regclass('public.pack_commerce_leads') is not null"))).scalar()
        )


async def _por_lotes(tabla: str, sql: str, tenant_id: uuid.UUID, lote: int) -> int:
    total = 0
    ultimo = uuid.UUID(int=0)
    while True:
        async with get_session() as session:
            ids = list(
                (await session.execute(text(_IDS[tabla]), {"tenant_id": tenant_id, "ultimo": ultimo, "lote": lote}))
                .scalars()
                .all()
            )
            if not ids:
                return total
            total += (await session.execute(text(sql), {"tenant_id": tenant_id, "ids": ids})).rowcount or 0
        ultimo = ids[-1]


async def _contar(sql: str, tenant_id: uuid.UUID) -> int:
    async with get_session() as session:
        return (await session.execute(text(sql), {"tenant_id": tenant_id})).scalar() or 0


async def _ocultos(tenant_id: uuid.UUID) -> list[LeadOculto]:
    async with get_session() as session:
        filas = (await session.execute(text(_OCULTOS_A_REVISAR), {"tenant_id": tenant_id})).all()
    return [LeadOculto(tenant_id, f.id, f.customer_id, f.status, f.next_action_at is not None) for f in filas]


async def _config_de_commerce(tenant_id: uuid.UUID) -> dict[str, Any] | None:
    async with get_session() as session:
        cifrada = (
            await session.execute(
                select(PluginConfig.config_encrypted).where(
                    PluginConfig.plugin_id == _COMMERCE,
                    PluginConfig.tenant_id == tenant_id,
                    PluginConfig.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if cifrada is None:
        return None
    datos = get_secret_box().decrypt_json(cifrada)
    return {k: v for k, v in datos.items() if k in _CAMPOS_DE_CONFIG}


async def _copiar_config(tenant_id: uuid.UUID, *, dry_run: bool) -> bool:
    """Crea la config de Captar leads desde la de commerce si el negocio aún no tiene. Nunca la pisa: una vez en el
    core, la edita el dueño ahí. Los secretos no pasan por el log."""
    datos = await _config_de_commerce(tenant_id)
    if datos is None:
        return False
    config = config_leads.desde_guardado(datos)
    async with get_session() as session:
        if await config_leads.cargar(session, tenant_id) is not None:
            return False
        if dry_run:
            return True
        await config_leads.guardar(session, tenant_id, config)
    return True


async def _copiar_negocio(tenant_id: uuid.UUID, reporte: ReporteCopia, lote: int) -> None:
    reporte.ocultos.extend(await _ocultos(tenant_id))
    if reporte.dry_run:
        reporte.leads += await _contar(_PENDIENTES["leads"], tenant_id)
        reporte.cambios += await _contar(_PENDIENTES["cambios"], tenant_id)
        reporte.seguimientos += await _contar(_PENDIENTES["seguimientos"], tenant_id)
    else:
        # Los leads primero: la historia y los seguimientos apuntan a ellos (FK lógica, como en commerce).
        reporte.leads += await _por_lotes("leads", _COPIAR_LEADS, tenant_id, lote)
        reporte.cambios += await _por_lotes("cambios", _COPIAR_CAMBIOS, tenant_id, lote)
        reporte.seguimientos += await _por_lotes("seguimientos", _COPIAR_SEGUIMIENTOS, tenant_id, lote)
    reporte.desactualizados += await _contar(_DESACTUALIZADOS, tenant_id)
    try:
        if await _copiar_config(tenant_id, dry_run=reporte.dry_run):
            reporte.configs += 1
    except (ValueError, RuntimeError, ValidationError):
        # Clave rotada, AAD distinto o esquema roto: no tumba la corrida; el negocio queda en el reporte.
        log.warning("copia_leads_config_ilegible", tenant_id=str(tenant_id))
        reporte.configs_ilegibles.append(tenant_id)


async def copiar_leads(
    *, dry_run: bool, tenant_id: uuid.UUID | None = None, lote: int = LOTE_POR_DEFECTO
) -> ReporteCopia:
    """Copia en todos los negocios (o en uno). Ver el docstring del módulo."""
    if lote < 1:
        raise ValueError("El lote tiene que ser al menos 1")
    reporte = ReporteCopia(dry_run=dry_run)
    if not await _hay_tablas_de_commerce():
        reporte.sin_tablas_de_commerce = True
        return reporte
    if tenant_id is not None:
        negocios = [tenant_id]
    else:
        async with get_session() as session:
            negocios = list(
                (await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)).order_by(Tenant.id)))
                .scalars()
                .all()
            )
    for negocio in negocios:
        async with tenant_scope(negocio):
            await _copiar_negocio(negocio, reporte, lote)
        reporte.negocios += 1
    log.info(
        "copia_leads",
        dry_run=dry_run,
        negocios=reporte.negocios,
        leads=reporte.leads,
        cambios=reporte.cambios,
        seguimientos=reporte.seguimientos,
        configs=reporte.configs,
        desactualizados=reporte.desactualizados,
        ocultos=[str(o.lead_id) for o in reporte.ocultos],
        configs_ilegibles=[str(t) for t in reporte.configs_ilegibles],
    )
    return reporte
