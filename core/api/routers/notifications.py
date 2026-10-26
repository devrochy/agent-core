"""Centro de notificaciones del admin (`/api/v1/me/notifications`, ADR-0167).

Feed in-app de los eventos del negocio activo. Sin gate de sección: cada admin lee solo lo que
ve (dueño y master, todo; colaborador, lo de las secciones con acceso explícito) y su estado
(leídas, archivadas, preferencias de push, silencio) es suyo. Tiempo real por polling del
contador (`/unread-count`) y de `?after=`.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.db.models import Notification
from core.db.session import get_session
from core.notifications import feed
from core.notifications.spec import CATEGORIAS, SEVERIDADES

router = APIRouter(prefix="/me/notifications", tags=["me · notificaciones"])

_MAX_SILENCIO_MIN = 7 * 24 * 60


class NotificationTarget(BaseModel):
    """Destino neutro: el cliente lo traduce a su propia ruta. `section` es la sección de PERMISO
    que gobierna la visibilidad (no una ruta). Un `entity` nuevo es aditivo: el cliente debe
    tener un valor por defecto."""

    entity: str = Field(description="conversacion, escalacion, lead, cliente, pedido, pago, suscripcion, cobro, cita, conexion, mensaje_fallido, cobranza")
    id: str | None
    section: str | None


class NotificationItem(BaseModel):
    id: uuid.UUID
    event_key: str
    category: Literal["conversaciones", "ventas", "cobros", "agenda", "sistema"]
    severity: Literal["info", "success", "warning", "critical"]
    title: str
    body: str
    target: NotificationTarget | None
    # Datos de apoyo (ids, claves heredadas del push): usar `target` y `event_key`, no `data`.
    data: dict[str, Any]
    created_at: datetime
    read: bool


class NotificationPage(BaseModel):
    items: list[NotificationItem]
    next_cursor: str | None
    # Todas las no leídas visibles para el usuario: no respeta los filtros `category` ni `unread`.
    unread_count: int


class UnreadCount(BaseModel):
    unread_count: int


class MarkReadRequest(BaseModel):
    ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    all: bool = False


class NotificationPreferences(BaseModel):
    categories: list[str]
    push_off_categories: list[str]
    muted_until: datetime | None


class PreferencesUpdate(BaseModel):
    push_off_categories: list[str] = Field(default_factory=list, max_length=len(CATEGORIAS))


class MuteRequest(BaseModel):
    """`minutes` > 0 silencia el push ese tiempo; `0` o ausente lo reactiva."""

    minutes: int | None = Field(default=None, ge=0, le=_MAX_SILENCIO_MIN)


def _negocio(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError(400, "sin_negocio", "Selecciona un negocio (X-Tenant-Id).")
    return principal.tenant_id


def _rol(principal: Principal) -> str | None:
    return next((r for (tid, _n, r) in principal.tenants if tid == principal.tenant_id), None)


def _cursor(n: Notification) -> str:
    """Opaco y seguro en una URL (el `+` de la zona horaria se perdería en una query)."""
    return base64.urlsafe_b64encode(f"{n.created_at.isoformat()}|{n.id}".encode()).decode()


def _parse_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, _, nid = base64.urlsafe_b64decode(raw.encode()).decode().partition("|")
        return datetime.fromisoformat(ts), uuid.UUID(nid)
    except ValueError as exc:
        raise ApiError(400, "cursor_invalido", "El cursor de paginación no es válido.") from exc


def _destino(target: dict | None) -> NotificationTarget | None:
    if not target:
        return None
    return NotificationTarget(entity=target["entity"], id=target.get("id"), section=target.get("section"))


def _out(n: Notification, leida: bool) -> NotificationItem:
    return NotificationItem(
        id=n.id,
        event_key=n.event_key,
        category=n.category,
        severity=n.severity,
        title=n.title,
        body=n.body,
        target=_destino(n.target),
        data=n.data or {},
        created_at=n.created_at,
        read=leida,
    )


async def _contexto(session, principal: Principal):
    """(negocio, secciones visibles, marca «leído hasta»)."""
    tid = _negocio(principal)
    secciones = await feed.secciones_visibles(session, tid, principal.admin, _rol(principal))
    estado = await feed.estado_de(session, tid, principal.admin.id)
    return tid, secciones, (estado.read_all_before if estado else None)


@router.get("", response_model=NotificationPage)
async def list_notifications(
    principal: Annotated[Principal, Depends(get_principal)],
    category: Annotated[str | None, Query()] = None,
    unread: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query()] = None,
    after: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> NotificationPage:
    if category is not None and category not in CATEGORIAS:
        raise ApiError(422, "categoria_invalida", "Categoría de notificación desconocida.")
    async with get_session() as session:
        tid, secciones, leida_hasta = await _contexto(session, principal)
        filas = await feed.listar(
            session,
            tid,
            principal.admin.id,
            secciones,
            leida_hasta,
            category=category,
            solo_no_leidas=unread,
            antes_de=_parse_cursor(cursor) if cursor else None,
            despues_de=after,
            limit=limit + 1,
        )
        no_leidas = await feed.contar_no_leidas(
            session, tid, principal.admin.id, secciones, leida_hasta
        )
    hay_mas = len(filas) > limit
    filas = filas[:limit]
    return NotificationPage(
        items=[_out(n, leida) for n, leida in filas],
        next_cursor=_cursor(filas[-1][0]) if hay_mas and filas else None,
        unread_count=no_leidas,
    )


@router.get("/unread-count", response_model=UnreadCount)
async def unread_count(principal: Annotated[Principal, Depends(get_principal)]) -> UnreadCount:
    async with get_session() as session:
        tid, secciones, leida_hasta = await _contexto(session, principal)
        n = await feed.contar_no_leidas(session, tid, principal.admin.id, secciones, leida_hasta)
    return UnreadCount(unread_count=n)


@router.post("/read", response_model=UnreadCount)
async def mark_read(
    payload: MarkReadRequest, principal: Annotated[Principal, Depends(get_principal)]
) -> UnreadCount:
    if not payload.all and not payload.ids:
        raise ApiError(422, "sin_ids", "Indica `ids` o `all: true`.")
    async with get_session() as session:
        tid, secciones, leida_hasta = await _contexto(session, principal)
        if payload.all:
            estado = await feed.obtener_o_crear_estado(session, tid, principal.admin.id)
            estado.read_all_before = datetime.now(UTC)
            leida_hasta = estado.read_all_before
        else:
            # Solo las que el admin ve: un id ajeno o de otra sección se ignora.
            visibles = (
                await session.execute(
                    select(Notification.id).where(
                        Notification.tenant_id == tid,
                        Notification.deleted_at.is_(None),
                        Notification.id.in_(payload.ids or []),
                        feed.filtro_de_visibilidad(secciones, principal.admin.id),
                    )
                )
            ).scalars().all()
            await feed.marcar_leidas(session, tid, principal.admin.id, visibles)
        await session.flush()
        n = await feed.contar_no_leidas(session, tid, principal.admin.id, secciones, leida_hasta)
    return UnreadCount(unread_count=n)


@router.delete("/{notification_id}", status_code=204)
async def archive_notification(
    notification_id: uuid.UUID, principal: Annotated[Principal, Depends(get_principal)]
) -> Response:
    async with get_session() as session:
        tid, secciones, _ = await _contexto(session, principal)
        existe = (
            await session.execute(
                select(Notification.id).where(
                    Notification.tenant_id == tid,
                    Notification.id == notification_id,
                    Notification.deleted_at.is_(None),
                    feed.filtro_de_visibilidad(secciones, principal.admin.id),
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise ApiError(404, "not_found", "La notificación no existe.")
        await feed.archivar(session, tid, principal.admin.id, notification_id)
    return Response(status_code=204)


def _prefs(estado) -> NotificationPreferences:
    ahora = datetime.now(UTC)
    muted = estado.muted_until if estado and estado.muted_until and estado.muted_until > ahora else None
    return NotificationPreferences(
        categories=list(CATEGORIAS),
        push_off_categories=list(estado.push_off_categories or []) if estado else [],
        muted_until=muted,
    )


@router.get("/preferences", response_model=NotificationPreferences)
async def get_preferences(
    principal: Annotated[Principal, Depends(get_principal)],
) -> NotificationPreferences:
    tid = _negocio(principal)
    async with get_session() as session:
        return _prefs(await feed.estado_de(session, tid, principal.admin.id))


@router.put("/preferences", response_model=NotificationPreferences)
async def put_preferences(
    payload: PreferencesUpdate, principal: Annotated[Principal, Depends(get_principal)]
) -> NotificationPreferences:
    tid = _negocio(principal)
    invalidas = [c for c in payload.push_off_categories if c not in CATEGORIAS]
    if invalidas:
        raise ApiError(422, "categoria_invalida", f"Categorías desconocidas: {', '.join(invalidas)}.")
    async with get_session() as session:
        estado = await feed.obtener_o_crear_estado(session, tid, principal.admin.id)
        estado.push_off_categories = sorted(set(payload.push_off_categories))
        return _prefs(estado)


@router.put("/mute", response_model=NotificationPreferences)
async def put_mute(
    payload: MuteRequest, principal: Annotated[Principal, Depends(get_principal)]
) -> NotificationPreferences:
    tid = _negocio(principal)
    async with get_session() as session:
        estado = await feed.obtener_o_crear_estado(session, tid, principal.admin.id)
        estado.muted_until = (
            datetime.now(UTC) + timedelta(minutes=payload.minutes) if payload.minutes else None
        )
        return _prefs(estado)


# Re-exporta para que el contrato del OpenAPI y los tests importen de un solo lugar.
__all__ = ["CATEGORIAS", "SEVERIDADES", "router"]
