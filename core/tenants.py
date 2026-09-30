"""Ciclo de vida de tenants: suspensión y purga (ADR-0001).

- `deactivate_tenant` / `activate_tenant`: toggle `tenants.status`
  (`active` | `suspended`). Un tenant suspendido deja de procesar mensajes en el
  webhook y el consumer, pero conserva sus datos y sigue visible en el panel
  para que el admin lo reactive.
- `purge_tenant`: borrado físico IRREVERSIBLE de todos los datos del tenant
  (core + packs + Redis + fila `tenants`). Es la excepción deliberada a la
  invariante #8 (sin borrado físico) del ciclo de vida del tenant (tipo GDPR):
  exige confirmación por frase (`ELIMINAR <slug>`) y rol master.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from core.db.models import Conversation, Message, Tenant
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

PURGE_PHRASE_PREFIX = "ELIMINAR"


def _validate_slug(slug: str) -> None:
    """Slug seguro (mismo criterio que onboarding). Evita traversal/typos en rutas."""
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        raise ValueError(f"Slug inválido (solo minúsculas, números y guiones): {slug!r}")


async def _resolve_tenant(ref) -> Tenant:
    ref = str(ref)
    async with get_session() as session:
        if len(ref) == 36:
            try:
                row = await session.get(Tenant, uuid.UUID(ref))
            except ValueError:
                row = None  # slug de 36 caracteres, no un UUID
        else:
            row = None
        if row is None:
            row = (
                await session.execute(select(Tenant).where(Tenant.slug == ref))
            ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Tenant no existe: {ref}")
        return row


async def is_tenant_active(tenant_id: uuid.UUID | None) -> bool:
    """¿El tenant puede procesar mensajes? (webhook + consumer, defensa en profundidad)."""
    if tenant_id is None:
        return False
    async with get_session() as session:
        row = await session.get(Tenant, tenant_id)
    return row is not None and row.deleted_at is None and row.status == "active"


async def resolve_tenant_by_slug(slug: str) -> uuid.UUID | None:
    """Id del tenant por slug si existe y está activo (webhook por slug)."""
    _validate_slug(slug)
    async with get_session() as session:
        row = (
            await session.execute(
                select(Tenant).where(
                    Tenant.slug == slug,
                    Tenant.deleted_at.is_(None),
                    Tenant.status == "active",
                )
            )
        ).scalar_one_or_none()
    return row.id if row is not None else None


def tenant_webhook_url(slug: str) -> str:
    """URL pública del webhook de WhatsApp para el tenant (`/webhooks/whatsapp/<slug>`).

    La registras en Meta (Callback URL). En desarrollo, `PUBLIC_BASE_URL` debe ser
    la URL del túnel (cloudflared), no localhost.
    """
    from core.config import get_settings

    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/webhooks/whatsapp/{slug}"


async def deactivate_tenant(ref: str) -> Tenant:
    """Suspende un tenant (status='suspended'): no procesa mensajes, conserva datos."""
    tenant = await _resolve_tenant(ref)
    async with get_session() as session:
        row = await session.get(Tenant, tenant.id)
        if row is not None and row.status != "suspended":
            row.status = "suspended"
            await session.commit()
    return await _resolve_tenant(tenant.id)


async def activate_tenant(ref: str) -> Tenant:
    """Re-activa un tenant suspendido (status='active')."""
    tenant = await _resolve_tenant(ref)
    async with get_session() as session:
        row = await session.get(Tenant, tenant.id)
        if row is not None and row.status != "active":
            row.status = "active"
            await session.commit()
    return await _resolve_tenant(tenant.id)


async def _clean_redis_for_tenant(tenant_id: uuid.UUID) -> None:
    """Borra el estado Redis del tenant (working memory, debounce, contadores).

    El stream `agent_core:inbound` no se toca: los mensajes pendientes del tenant
    los descarta el guard del consumer (`is_tenant_active`). Los claims de
    idempotencia `inbound:*` del tenant se limpian desde `messages.idempotency_key`.
    """
    from core.memory.working import get_redis
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            conversations = (
                await session.execute(
                    select(Conversation.id).where(Conversation.tenant_id == tenant_id)
                )
            ).scalars().all()
            idempotency_keys = (
                await session.execute(
                    select(Message.idempotency_key).where(
                        Message.tenant_id == tenant_id,
                        Message.idempotency_key.is_not(None),
                    )
                )
            ).scalars().all()

    keys = [f"wm:{c}" for c in conversations]
    keys += [f"debounce:payload:{c}" for c in conversations]
    keys += [f"turns:{tenant_id}"]
    keys += [f"inbound:meta:{ik}" for ik in idempotency_keys]
    if not keys:
        return

    r = get_redis()
    try:
        await r.delete(*keys)
        if conversations:
            await r.zrem("debounce:pending", *[str(c) for c in conversations])
    except Exception:
        log.warning("tenant_redis_cleanup_failed", tenant_id=str(tenant_id))


async def purge_tenant(ref: str, confirm_phrase: str) -> None:
    """Borra TODO el tenant de forma irreversible.

    Exige que `confirm_phrase` sea exactamente `ELIMINAR <slug>`. Borra datos de
    todas las tablas con `tenant_id` (core + packs), la fila `tenants`, y el
    estado Redis. La función SQL `purge_tenant(tid)` es SECURITY DEFINER (dueño,
    omite RLS) y transaccional: si algo falla, no queda a medias.
    """
    tenant = await _resolve_tenant(ref)
    expected = f"{PURGE_PHRASE_PREFIX} {tenant.slug}"
    if confirm_phrase.strip() != expected:
        raise ValueError(f"Frase de confirmación incorrecta. Escribe exactamente: {expected}")

    await _clean_redis_for_tenant(tenant.id)

    async with get_session() as session:
        await session.execute(text("select purge_tenant(:tid)"), {"tid": str(tenant.id)})
        await session.commit()

    # Verificación: el tenant ya no existe.
    gone = await _resolve_tenant_if_exists(tenant.id)
    if gone is not None:
        raise RuntimeError("purge_tenant no eliminó el tenant (ver logs)")


async def _resolve_tenant_if_exists(ref) -> Tenant | None:
    ref = str(ref)
    async with get_session() as session:
        if len(ref) == 36:
            return await session.get(Tenant, uuid.UUID(ref))
        return (
            await session.execute(select(Tenant).where(Tenant.slug == ref))
        ).scalar_one_or_none()