"""Ciclo de vida de tenants: suspensión y purga (ADR-0001).

- `deactivate_tenant` / `activate_tenant`: toggle `tenants.status`
  (`active` | `suspended`). Un tenant suspendido deja de procesar mensajes en el
  webhook y el consumer, pero conserva sus datos y sigue visible en el panel
  para que el admin lo reactive.
- `purge_tenant`: borrado físico IRREVERSIBLE de todos los datos del tenant
  (core + packs + fila `tenants`, transaccional) y, después del commit, de su
  estado Redis (reintento: `purge_tenant_redis`). Es la excepción deliberada a la
  invariante #8 (sin borrado físico) del ciclo de vida del tenant (tipo GDPR):
  exige que el tenant esté suspendido (`status='suspended'`), confirmación por
  frase (`ELIMINAR <slug>`) y rol master.
"""

from __future__ import annotations

import asyncio
import re
import uuid

from sqlalchemy import select, text

from core.db.models import Conversation, Tenant
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

PURGE_PHRASE_PREFIX = "ELIMINAR"


class TenantNotSuspendedError(ValueError):
    """Se intentó purgar un tenant que no está suspendido (`status != 'suspended'`)."""

    def __init__(self, slug: str) -> None:
        super().__init__(
            f"El negocio {slug} no está suspendido: suspéndelo primero "
            f"(agent-core tenant deactivate {slug}, o Desactivar en el panel) y vuelve a intentarlo."
        )
        self.slug = slug


def require_suspended_for_purge(tenant: Tenant) -> None:
    """Solo se purga un tenant suspendido (ciclo de vida, `tenants.status`).

    Es la suspensión del ciclo de vida (`deactivate_tenant`), no la falta de pago
    (`tenant_subscriptions`, ADR-0045): un tenant `active` sin pago no es purgable.
    La función SQL `purge_tenant` lo vuelve a exigir (defensa en profundidad).
    """
    if tenant.status != "suspended":
        raise TenantNotSuspendedError(tenant.slug)


async def ensure_purgeable(ref: str) -> Tenant:
    """Resuelve el tenant y exige que se pueda purgar (para avisar antes de pedir la frase)."""
    tenant = await _resolve_tenant(ref)
    require_suspended_for_purge(tenant)
    return tenant


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


_REDIS_LOTE = 500


async def _conversaciones_del_tenant(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Ids de conversación del tenant (lectura, ANTES de purgar: después ya no existen).

    Hacen falta para las claves Redis que van solo por conversación, sin el tenant en
    el nombre: `wm:{conv}` (formato legado, sin tenant en contexto),
    `debounce:payload:{conv}`, `debounce:messages:{conv}` y el miembro `{conv}` del ZSET
    `debounce:pending`.
    """
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return list(
                (
                    await session.execute(
                        select(Conversation.id).where(Conversation.tenant_id == tenant_id)
                    )
                ).scalars().all()
            )


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Familias que van solo por conversación (sin el tenant en el nombre). El barrido no las
# toca: se borran por id de conversación (leídos antes de purgar) o vencen solas.
_SOLO_CONVERSACION = ("debounce:payload:", "debounce:messages:")


def _es_del_tenant(clave: str, tid: str) -> bool | None:
    """¿La clave es estado del tenant `tid` según la convención `<familia>:{tenant_id}:...`?

    True: el primer UUID del nombre es `tid` (posición de tenant). None: lo lleva pero en
    otra posición (es el id de un hijo —conversación, conexión, mensaje— de OTRO negocio, o
    una clave de varios negocios): no es suya. False: familia solo por conversación o
    `wm:{conv}` legado (un solo UUID), que el barrido no toca.
    """
    uuids = _UUID_RE.findall(clave)
    if not uuids or uuids[0] != tid:
        return None
    if clave.startswith(_SOLO_CONVERSACION) or (clave.startswith("wm:") and len(uuids) == 1):
        return False
    return True


async def _limpiar_redis(
    tenant_id: uuid.UUID, conversation_ids: list[uuid.UUID], *, reintento: bool = False
) -> int:
    """Borra el estado Redis del tenant. Idempotente; lanza si Redis falla.

    Dos partes:
    - claves por conversación (`conversation_ids`, recolectados antes de la purga):
      `wm:{conv}` legado, `debounce:payload|messages:{conv}` y el miembro de `debounce:pending`;
    - `SCAN MATCH *<tenant_id>*` y, de lo que encuentra, solo las claves con el UUID en
      posición de tenant (primer UUID del nombre): `wm:{tid}:*`, `turns:{tid}`,
      `inbound:{tid}:*`, `alta:terminos:{tid}:*`, `waweb:*:{tid}`, `meta:paused:{tid}:*`,
      `outbound:claim:{tid}:*`... y las familias que se agreguen con la misma convención.

    Con `reintento=True`, si alguna clave lleva el UUID en otra posición (el operador pegó
    el id de una conversación, conexión o mensaje de un negocio vivo), se niega sin borrar
    nada (`ValueError`).

    Convención en la que se apoya (docs/MULTITENANT.md): todo estado Redis de un tenant
    lleva su UUID como primer UUID del nombre; las claves solo por conversación tienen TTL
    de 24 h o menos. Los streams no van por nombre (el tenant va en los campos): de la DLQ
    se borran sus entradas (`XDEL` por el campo `tenant_id`); `agent_core:inbound` no se
    toca (lo procesado ya se borró al ackear y lo pendiente lo descarta el consumer).
    Devuelve claves + entradas de la DLQ borradas.
    """
    from core.memory.working import get_redis

    r = get_redis()
    tid = str(tenant_id)
    borradas = 0
    por_conversacion = [
        clave
        for c in conversation_ids
        for clave in (f"wm:{c}", f"debounce:payload:{c}", f"debounce:messages:{c}")
    ]
    for i in range(0, len(por_conversacion), _REDIS_LOTE):
        borradas += int(await r.unlink(*por_conversacion[i : i + _REDIS_LOTE]))
    miembros = [str(c) for c in conversation_ids]
    for i in range(0, len(miembros), _REDIS_LOTE):
        await r.zrem("debounce:pending", *miembros[i : i + _REDIS_LOTE])

    suyas: list[str] = []
    ajenas = 0
    async for clave in r.scan_iter(match=f"*{tid}*", count=1000):
        es = _es_del_tenant(clave, tid)
        if es:
            suyas.append(clave)
        elif es is None:
            ajenas += 1
    if reintento and ajenas:
        raise ValueError(
            f"{tid} aparece en {ajenas} clave(s) de Redis en una posición que no es la de un "
            "negocio (parece el id de una conversación, conexión o mensaje de otro negocio): "
            "no se borra nada. Revisa el UUID del log `tenant_purge_redis_cleanup_failed`."
        )
    for i in range(0, len(suyas), _REDIS_LOTE):
        borradas += int(await r.unlink(*suyas[i : i + _REDIS_LOTE]))
    # Sus entradas de la DLQ (texto y teléfono de sus clientes, ADR-0053): no esperan
    # a que venza la retención de la DLQ.
    from core.orchestrator.consumer import DEAD_LETTER_STREAM
    from core.orchestrator.retencion_streams import borrar_dlq_del_tenant

    borradas += await borrar_dlq_del_tenant(r, DEAD_LETTER_STREAM, tid)
    return borradas


async def _limpiar_redis_de_inexistente(tid: uuid.UUID, *, reintento: bool) -> int:
    if await _resolve_tenant_if_exists(tid) is not None:
        raise ValueError(
            f"El negocio {tid} todavía existe: este comando solo limpia Redis de un negocio "
            "ya purgado (usa `agent-core tenant purge`)."
        )
    return await _limpiar_redis(tid, [], reintento=reintento)


async def purge_tenant_redis(tenant_id: uuid.UUID | str) -> int:
    """Reintento: limpia el estado Redis de un tenant YA purgado. Idempotente.

    Para cuando `purge_tenant` borró la base pero la limpieza de Redis falló
    (`tenant_purge_redis_cleanup_failed` en los logs). Solo acepta el UUID (el slug ya no
    existe) y se NIEGA (`ValueError`, sin borrar nada) si el tenant aún existe o si el UUID
    aparece en Redis como id de un hijo de otro negocio (error de copiar y pegar). Cubre
    todo lo que lleva el UUID del tenant en posición de tenant; las claves solo por
    conversación ya no se pueden enumerar (la purga borró las conversaciones) y vencen
    solas (debounce 5 min, `wm:{conv}` 24 h). Devuelve cuántas claves (y entradas de la DLQ) borró.
    """
    tid = uuid.UUID(str(tenant_id))  # normaliza (MATCH distingue mayúsculas)
    borradas = await _limpiar_redis_de_inexistente(tid, reintento=True)
    log.info("tenant_purge_redis_retry_done", tenant_id=str(tid), keys_deleted=borradas)
    return borradas


async def limpiar_redis_antes_del_alta(tenant_id: uuid.UUID) -> int:
    """Borra el estado Redis que haya quedado con el id de un negocio que aún NO existe.

    Para el alta automática (ADR-0121), cuyo id es determinista: un negocio anterior de la
    misma alta, purgado sin limpiar Redis, dejaría claves que el nuevo heredaría. Es la
    limpieza de `purge_tenant_redis`, sin su negativa por el UUID en otra posición: esa
    detecta un id pegado por una persona, y este lo calcula el core (esas claves son de
    otros negocios y no se tocan). SOLO para ids que calcula el core, nunca uno que llegue
    de una persona o de la API. `ValueError` si el negocio existe.
    """
    return await _limpiar_redis_de_inexistente(tenant_id, reintento=False)


async def purge_tenant(ref: str, confirm_phrase: str) -> bool:
    """Borra TODO el tenant de forma irreversible.

    Exige que el tenant esté suspendido (`TenantNotSuspendedError` si no) y que
    `confirm_phrase` sea exactamente `ELIMINAR <slug>`. Borra datos de
    todas las tablas con `tenant_id` (core + packs) y la fila `tenants` con la función
    SQL `purge_tenant(tid)`: SECURITY DEFINER (dueño, omite RLS) y transaccional; si
    algo falla, no queda a medias y Redis queda intacto.

    El estado Redis se limpia DESPUÉS del commit (Redis no entra en la transacción).
    Si esa limpieza falla, la purga NO se revierte (la base ya no tiene el negocio): se
    registra `tenant_purge_redis_cleanup_failed` con el tenant, se alerta y se devuelve
    False; el reintento es `agent-core tenant purge-redis <tenant_id>`. True = todo limpio.
    """
    tenant = await _resolve_tenant(ref)
    require_suspended_for_purge(tenant)
    expected = f"{PURGE_PHRASE_PREFIX} {tenant.slug}"
    if confirm_phrase.strip() != expected:
        raise ValueError(f"Frase de confirmación incorrecta. Escribe exactamente: {expected}")

    # Solo lectura: después de la purga ya no se pueden enumerar.
    conversation_ids = await _conversaciones_del_tenant(tenant.id)

    async with get_session() as session:
        await session.execute(text("select purge_tenant(:tid)"), {"tid": str(tenant.id)})
        await session.commit()

    # Verificación: el tenant ya no existe. Si sigue, Redis no se toca.
    gone = await _resolve_tenant_if_exists(tenant.id)
    if gone is not None:
        raise RuntimeError("purge_tenant no eliminó el tenant (ver logs)")

    retry = f"agent-core tenant purge-redis {tenant.id}"
    try:
        borradas = await _limpiar_redis(tenant.id, conversation_ids)
    except asyncio.CancelledError:
        # La base ya está purgada: que la cancelación (p. ej. la petición HTTP) no deje
        # Redis a medias en silencio. Sin alerta (no se puede esperar nada más aquí).
        log.error(
            "tenant_purge_redis_cleanup_failed", tenant_id=str(tenant.id), retry=retry,
            cancelled=True,
        )
        raise
    except Exception:
        # Sin slug (puede derivar del nombre de una persona): basta el UUID.
        log.error(
            "tenant_purge_redis_cleanup_failed",
            tenant_id=str(tenant.id),
            retry=retry,
            exc_info=True,
        )
        try:
            from core.shared.alerts import send_alert

            await send_alert(
                "Purga de negocio: Redis sin limpiar",
                f"El negocio {tenant.id} se purgó de la base, pero su estado Redis no se pudo "
                f"limpiar. Reintenta con: {retry} (lo que va solo por conversación vence solo "
                "en 24 h o menos).",
                severity="critical",
            )
        except Exception:
            log.exception("tenant_purge_redis_alert_failed", tenant_id=str(tenant.id))
        return False
    log.info("tenant_purged", tenant_id=str(tenant.id), redis_keys_deleted=borradas)
    return True


async def _resolve_tenant_if_exists(ref) -> Tenant | None:
    ref = str(ref)
    async with get_session() as session:
        if len(ref) == 36:
            return await session.get(Tenant, uuid.UUID(ref))
        return (
            await session.execute(select(Tenant).where(Tenant.slug == ref))
        ).scalar_one_or_none()