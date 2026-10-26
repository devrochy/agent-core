"""Lectura del feed de notificaciones por admin: visibilidad, no leídas y estado (ADR-0167).

Una notificación es del negocio, no de una persona: la ve el dueño, el master (que opera el
negocio seleccionado) y el colaborador con acceso EXPLÍCITO a su `section` (heredar `read` por
defecto no da avisos, mismo criterio que `notification_recipients`, ADR-0136). El estado de
lectura es por admin (`notification_reads`, `notification_state`).

Todas las funciones reciben la sesión del llamador y asumen `tenant_scope` abierto.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, false, func, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.admin.permissions import resolver_explicito
from core.admin.roles import actua_como_master
from core.db.models import (
    Admin,
    AdminPermission,
    Notification,
    NotificationRead,
    NotificationState,
    TenantUser,
)


def ve_notificacion(
    *,
    actua_master: bool,
    tenant_role: str | None,
    section: str | None,
    explicitos: Mapping[str, Iterable[str]],
) -> bool:
    """¿Este admin ve una notificación de `section`? Función pura.

    Master (o tester habilitado) y dueño ven todo. Un colaborador, solo con una fila
    explícita que le dé acceso a la sección (sub-sección > pack; `none` deniega). Sin rol en
    el negocio y sin ser master, no ve nada. `section=None` = solo dueños y masters.
    """
    if actua_master or tenant_role == "owner":
        return True
    if tenant_role is None or section is None:
        return False
    hay_fila, acceso = resolver_explicito(explicitos, section)
    return hay_fila and acceso is not None


async def explicitos_de(
    session, tenant_id: uuid.UUID, admin_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, dict[str, list[str]]]:
    """Filas de `admin_permissions` aplicables (globales o del negocio), por admin y sección."""
    ids = list(admin_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(AdminPermission.admin_id, AdminPermission.section, AdminPermission.access).where(
                AdminPermission.admin_id.in_(ids),
                AdminPermission.tenant_id.is_(None) | (AdminPermission.tenant_id == tenant_id),
            )
        )
    ).all()
    out: dict[uuid.UUID, dict[str, list[str]]] = {}
    for admin_id, section, access in rows:
        out.setdefault(admin_id, {}).setdefault(section, []).append(access)
    return out


async def rol_en_negocio(session, tenant_id: uuid.UUID, admin_ids: Iterable[uuid.UUID]) -> dict:
    ids = list(admin_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(TenantUser.admin_id, TenantUser.role).where(
                TenantUser.tenant_id == tenant_id, TenantUser.admin_id.in_(ids)
            )
        )
    ).all()
    return dict(rows)


async def secciones_visibles(
    session, tenant_id: uuid.UUID, admin: Admin, tenant_role: str | None
) -> set[str] | None:
    """Secciones cuyas notificaciones ve el admin; `None` = todas (dueño o master)."""
    if actua_como_master(admin) or tenant_role == "owner":
        return None
    if tenant_role is None:
        return set()
    presentes = (
        await session.execute(
            select(Notification.section)
            .where(Notification.tenant_id == tenant_id, Notification.deleted_at.is_(None))
            .distinct()
        )
    ).scalars().all()
    explicitos = (await explicitos_de(session, tenant_id, [admin.id])).get(admin.id, {})
    return {
        s
        for s in presentes
        if s is not None
        and ve_notificacion(
            actua_master=False, tenant_role=tenant_role, section=s, explicitos=explicitos
        )
    }


def filtro_de_visibilidad(secciones: set[str] | None) -> ColumnElement[bool]:
    if secciones is None:
        return true()
    if not secciones:
        return false()
    return Notification.section.in_(sorted(secciones))


async def estado_de(session, tenant_id: uuid.UUID, admin_id: uuid.UUID) -> NotificationState | None:
    return (
        await session.execute(
            select(NotificationState).where(
                NotificationState.tenant_id == tenant_id,
                NotificationState.admin_id == admin_id,
                NotificationState.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def obtener_o_crear_estado(
    session, tenant_id: uuid.UUID, admin_id: uuid.UUID
) -> NotificationState:
    estado = await estado_de(session, tenant_id, admin_id)
    if estado is None:
        estado = NotificationState(tenant_id=tenant_id, admin_id=admin_id, push_off_categories=[])
        session.add(estado)
        await session.flush()
    return estado


def _condiciones_no_leida(
    leida_hasta: datetime | None,
) -> list[ColumnElement[bool]]:
    cond: list[ColumnElement[bool]] = [
        NotificationRead.read_at.is_(None),
        NotificationRead.archived_at.is_(None),
    ]
    if leida_hasta is not None:
        cond.append(Notification.created_at > leida_hasta)
    return cond


def _con_lectura(tenant_id: uuid.UUID, admin_id: uuid.UUID):
    return and_(
        NotificationRead.notification_id == Notification.id,
        NotificationRead.tenant_id == tenant_id,
        NotificationRead.admin_id == admin_id,
    )


async def contar_no_leidas(
    session,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    secciones: set[str] | None,
    leida_hasta: datetime | None,
) -> int:
    stmt = (
        select(func.count())
        .select_from(Notification)
        .outerjoin(NotificationRead, _con_lectura(tenant_id, admin_id))
        .where(
            Notification.tenant_id == tenant_id,
            Notification.deleted_at.is_(None),
            filtro_de_visibilidad(secciones),
            *_condiciones_no_leida(leida_hasta),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def listar(
    session,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    secciones: set[str] | None,
    leida_hasta: datetime | None,
    *,
    category: str | None = None,
    solo_no_leidas: bool = False,
    antes_de: tuple[datetime, uuid.UUID] | None = None,
    despues_de: datetime | None = None,
    limit: int = 30,
) -> list[tuple[Notification, bool]]:
    """Notificaciones visibles (más nuevas primero) con su flag `leída`; excluye archivadas."""
    stmt = (
        select(Notification, NotificationRead.read_at)
        .outerjoin(NotificationRead, _con_lectura(tenant_id, admin_id))
        .where(
            Notification.tenant_id == tenant_id,
            Notification.deleted_at.is_(None),
            NotificationRead.archived_at.is_(None),
            filtro_de_visibilidad(secciones),
        )
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
    )
    if category:
        stmt = stmt.where(Notification.category == category)
    if solo_no_leidas:
        stmt = stmt.where(*_condiciones_no_leida(leida_hasta))
    if antes_de is not None:
        creada, nid = antes_de
        stmt = stmt.where(
            (Notification.created_at < creada)
            | ((Notification.created_at == creada) & (Notification.id < nid))
        )
    if despues_de is not None:
        stmt = stmt.where(Notification.created_at > despues_de)
    filas = (await session.execute(stmt)).all()
    return [
        (n, read_at is not None or (leida_hasta is not None and n.created_at <= leida_hasta))
        for n, read_at in filas
    ]


async def marcar_leidas(
    session, tenant_id: uuid.UUID, admin_id: uuid.UUID, notification_ids: Iterable[uuid.UUID]
) -> int:
    """Upsert de `read_at` para las notificaciones dadas (idempotente)."""
    ahora = datetime.now(UTC)
    n = 0
    for nid in notification_ids:
        stmt = (
            pg_insert(NotificationRead)
            .values(
                tenant_id=tenant_id, admin_id=admin_id, notification_id=nid, read_at=ahora
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "admin_id", "notification_id"],
                set_={"read_at": func.coalesce(NotificationRead.read_at, ahora)},
            )
        )
        await session.execute(stmt)
        n += 1
    return n


async def archivar(
    session, tenant_id: uuid.UUID, admin_id: uuid.UUID, notification_id: uuid.UUID
) -> None:
    ahora = datetime.now(UTC)
    stmt = (
        pg_insert(NotificationRead)
        .values(
            tenant_id=tenant_id,
            admin_id=admin_id,
            notification_id=notification_id,
            read_at=ahora,
            archived_at=ahora,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "admin_id", "notification_id"],
            set_={
                "read_at": func.coalesce(NotificationRead.read_at, ahora),
                "archived_at": ahora,
            },
        )
    )
    await session.execute(stmt)


def limite_de_retencion(ahora: datetime | None = None, dias: int = 180) -> datetime:
    return (ahora or datetime.now(UTC)) - timedelta(days=dias)
