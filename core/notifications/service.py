"""Emisión de notificaciones del negocio y entrega del push (ADR-0167).

Dos formas de emitir, la misma garantía:

- `notify(session, tenant_id, spec)`: dentro de la transacción del hecho (un pago, una cita).
  Inserta la fila y escribe `notification.created` en el outbox en un SAVEPOINT y **nunca
  propaga**: un texto mal armado o una violación de unicidad no puede revertir un cobro.
- `notify_standalone(tenant_id, spec)`: abre su propio `tenant_scope` y sesión (disparadores
  fuera de una transacción, p. ej. escalación o una pausa de conexión).

El push sale del handler core de `notification.created` (`enviar_push_de_notificacion`):
resuelve a los destinatarios, respeta preferencias y silencio, y es idempotente por
`push_sent_at` (el outbox entrega at-least-once).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.admin.roles import actua_como_master
from core.db.models import Admin, Notification, NotificationState, PushToken
from core.db.session import get_session
from core.events.outbox import emit_domain
from core.notifications import feed
from core.notifications.push import enviar_push
from core.notifications.spec import NotificationSpec
from core.shared.events import ON_NOTIFICATION_CREATED
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)


async def notify(session, tenant_id: uuid.UUID, spec: NotificationSpec) -> uuid.UUID | None:
    """Registra la notificación en la sesión del llamador. Devuelve su id, o `None` si ya
    existía (dedupe) o falló. Nunca lanza."""
    try:
        spec.validate()
        async with session.begin_nested():
            stmt = (
                pg_insert(Notification)
                .values(
                    tenant_id=tenant_id,
                    event_key=spec.event_key,
                    category=spec.category,
                    severity=spec.severity,
                    section=spec.section,
                    audience_admin_id=spec.audience_admin_id,
                    title=spec.title,
                    body=spec.body,
                    data=spec.data,
                    target=spec.target,
                    dedupe_key=spec.dedupe_key,
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "dedupe_key"],
                    index_where=text("deleted_at IS NULL"),
                )
                .returning(Notification.id)
            )
            nid = (await session.execute(stmt)).scalar_one_or_none()
            if nid is None:
                log.info("notificacion_duplicada", event_key=spec.event_key)
                return None
            await emit_domain(
                session,
                tenant_id=tenant_id,
                topic=ON_NOTIFICATION_CREATED,
                dedupe_key=f"notificacion:{nid}",
                payload={"notification_id": str(nid)},
            )
        return nid
    except Exception as exc:
        # Solo el tipo del error: un error de SQLAlchemy trae los parámetros (título, cuerpo).
        log.warning("notificacion_fallo", event_key=getattr(spec, "event_key", "?"), error=type(exc).__name__)
        return None


async def notify_by_actor(
    session, tenant_id: uuid.UUID, spec: NotificationSpec, actor_admin_id: uuid.UUID
) -> uuid.UUID | None:
    """Como `notify`, para un hecho que hizo una persona del equipo: a ella no le llega el
    push (`destinatarios_de_push` lee `data.actorAdminId`) y el aviso nace leído para ella.
    Nunca lanza."""
    nid = await notify(session, tenant_id, spec)
    if nid is None:
        return None
    try:
        async with session.begin_nested():
            await feed.marcar_leidas(session, tenant_id, actor_admin_id, [nid])
    except Exception as exc:
        log.warning("notificacion_actor_fallo", event_key=spec.event_key, error=type(exc).__name__)
    return nid


async def notify_standalone(tenant_id: uuid.UUID, spec: NotificationSpec) -> uuid.UUID | None:
    """Como `notify`, con su propio scope y sesión. Nunca lanza.

    Lo urgente (severidad `warning` o `critical`) no espera al despachador del outbox (30 s):
    tras el commit encola el envío inmediato; `push_sent_at` lo mantiene idempotente.
    """
    try:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                nid = await notify(session, tenant_id, spec)
    except Exception as exc:
        log.warning("notificacion_standalone_fallo", event_key=spec.event_key, error=type(exc).__name__)
        return None
    if nid is not None and spec.severity in ("warning", "critical"):
        try:
            from core.tasks.push_tasks import send_notification_push

            send_notification_push.delay(str(tenant_id), str(nid))
        except Exception as exc:
            log.warning("notificacion_envio_inmediato_fallo", event_key=spec.event_key, error=type(exc).__name__)
    return nid


def notify_standalone_sync(tenant_id: uuid.UUID, spec: NotificationSpec) -> None:
    """Para contextos síncronos (tareas Celery, hilos): corre `notify_standalone` en el loop
    persistente del proceso. Nunca lanza."""
    from core.shared.async_bridge import run_async

    try:
        run_async(notify_standalone(tenant_id, spec))
    except Exception as exc:
        log.warning("notificacion_sync_fallo", event_key=spec.event_key, error=type(exc).__name__)


async def destinatarios_de_push(
    session, notificacion: Notification
) -> tuple[list[uuid.UUID], dict[uuid.UUID, int]]:
    """Admins que deben recibir el push de la notificación y su contador de no leídas.

    Candidatos: quienes tienen un dispositivo activo registrado en el negocio. De ellos, los que
    VEN la notificación (dueño, master del negocio con dispositivo registrado, colaborador con
    acceso explícito a la sección), sin silencio vigente ni la categoría apagada.
    """
    tid = notificacion.tenant_id
    candidatos = (
        await session.execute(
            select(PushToken.admin_id)
            .where(PushToken.tenant_id == tid, PushToken.deleted_at.is_(None))
            .distinct()
        )
    ).scalars().all()
    if not candidatos:
        return [], {}

    admins = {
        a.id: a
        for a in (
            await session.execute(
                select(Admin).where(Admin.id.in_(candidatos), Admin.active.is_(True))
            )
        ).scalars()
    }
    roles = await feed.rol_en_negocio(session, tid, admins.keys())
    explicitos = await feed.explicitos_de(session, tid, admins.keys())
    estados = {
        e.admin_id: e
        for e in (
            await session.execute(
                select(NotificationState).where(
                    NotificationState.tenant_id == tid,
                    NotificationState.admin_id.in_(list(admins)),
                    NotificationState.deleted_at.is_(None),
                )
            )
        ).scalars()
    }

    ahora = datetime.now(UTC)
    elegidos: list[uuid.UUID] = []
    badges: dict[uuid.UUID, int] = {}
    actor = (notificacion.data or {}).get("actorAdminId")
    for admin_id, admin in admins.items():
        if actor is not None and str(admin_id) == str(actor):
            continue
        if not feed.ve_notificacion(
            actua_master=actua_como_master(admin),
            tenant_role=roles.get(admin_id),
            section=notificacion.section,
            explicitos=explicitos.get(admin_id, {}),
            es_destinatario=notificacion.audience_admin_id == admin_id,
        ):
            continue
        estado = estados.get(admin_id)
        if estado is not None:
            if estado.muted_until is not None and estado.muted_until > ahora:
                continue
            if notificacion.category in (estado.push_off_categories or []):
                continue
        elegidos.append(admin_id)
        secciones = await feed.secciones_visibles(session, tid, admin, roles.get(admin_id))
        badges[admin_id] = await feed.contar_no_leidas(
            session, tid, admin_id, secciones, estado.read_all_before if estado else None
        )
    return elegidos, badges


def _payload_push(n: Notification) -> dict[str, Any]:
    """`data` del push: lo legacy de la spec (`type`/`kind`, ids) más el contrato nuevo."""
    return {
        **(n.data or {}),
        "notificationId": str(n.id),
        "category": n.category,
        "severity": n.severity,
        "target": n.target,
    }


async def enviar_push_de_notificacion(notification_id: uuid.UUID) -> None:
    """Envía el push de una notificación una sola vez (idempotente por `push_sent_at`).

    Corre bajo el `tenant_scope` del negocio. Si el servicio de Expo falla, levanta
    `PushTransportError` y el outbox reintenta con backoff; `push_sent_at` solo se marca
    cuando el envío terminó, así un reintento no repite lo ya entregado.
    """
    async with get_session() as session:
        n = (
            await session.execute(
                select(Notification).where(
                    Notification.id == notification_id,
                    Notification.tenant_id == get_current_tenant_id(),
                    Notification.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if n is None or n.push_sent_at is not None:
            return
        admin_ids, badges = await destinatarios_de_push(session, n)
        tenant_id, title, body = n.tenant_id, n.title, n.body
        category, severity, data = n.category, n.severity, _payload_push(n)

    if admin_ids:
        await enviar_push(
            tenant_id,
            admin_ids,
            title,
            body,
            data,
            category=category,
            severity=severity,
            badges=badges,
        )

    async with get_session() as session:
        await session.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.tenant_id == tenant_id,
                Notification.push_sent_at.is_(None),
            )
            .values(push_sent_at=datetime.now(UTC))
        )
        await session.commit()


async def on_notification_created(**payload: Any) -> None:
    """Handler core del outbox: `notification.created` → push."""
    nid = payload.get("notification_id")
    if not nid:
        return
    await enviar_push_de_notificacion(uuid.UUID(str(nid)))
