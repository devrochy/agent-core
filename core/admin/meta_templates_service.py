"""Plantillas de WhatsApp de una conexión Meta (fuente única SSR + API).

- `list_templates`: lista las plantillas del WABA desde la base, siempre; antes sincroniza con
  Meta si le toca el turno horario (candado compartido con el cron, HU-CONN-01).
- `sync_templates_now`: el botón «Sincronizar» del panel. NO usa el candado horario (P0 de
  pruebas 2026-10-17: «ya se sincronizó en la última hora» escondía una plantilla recién
  creada); tiene su propio límite de una vez por minuto por negocio y WABA.
- `delete_template`: borra TODAS las versiones de idioma. Si Meta no lo permite (sus
  plantillas de ejemplo responden `(#100) Invalid parameter`), devuelve un error con código
  propio y la fila NO se marca eliminada.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.admin.meta_numbers_service import resolve_waba_and_credentials
from core.db.models import MessageTemplate
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

# Límite del botón manual: una sincronización por minuto por negocio y WABA. La llave lleva
# el tenant porque `waba_id` no es único entre negocios (mismo criterio que el candado horario).
MANUAL_SYNC_RATE_LIMIT_S = 60
MANUAL_SYNC_RATE_LIMITED = "Ya se sincronizó hace menos de un minuto: espera un momento y vuelve a intentarlo."
# Código 100 de Graph: «Invalid parameter». En un DELETE de plantilla es lo que responde Meta
# cuando no permite borrarla (sus plantillas de ejemplo, p. ej. `hello_world`).
_GRAPH_INVALID_PARAMETER = 100
_NOMBRES_DE_EJEMPLO = ("hello_world", "sample_", "jaspers_market_")


@dataclass(frozen=True)
class TemplateError:
    """Error legible con código estable para la API (`code`) y texto para la persona."""

    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _manual_sync_key(tenant_id: uuid.UUID, waba_id: str) -> str:
    return f"tpl_sync_manual:{tenant_id}:{waba_id}"


def es_plantilla_de_ejemplo(name: str) -> bool:
    """¿Es una de las plantillas que Meta crea de muestra en cada WABA?"""
    nombre = (name or "").lower()
    return nombre == "hello_world" or any(nombre.startswith(p) for p in _NOMBRES_DE_EJEMPLO[1:])


def mensaje_no_eliminable(name: str) -> str:
    if es_plantilla_de_ejemplo(name):
        return (
            f"Meta no permite eliminar la plantilla de ejemplo «{name}»: la crea Meta en cada cuenta "
            "y solo se puede quitar desde el WhatsApp Manager. Puedes dejarla; no afecta a las tuyas."
        )
    return (
        f"Meta no permite eliminar la plantilla «{name}» por la API. Revisa en el WhatsApp Manager "
        "si sigue existiendo o si Meta la bloqueó."
    )


async def list_templates(
    tenant_id: uuid.UUID, connection_id: str
) -> tuple[list[dict], str | None, bool]:
    """(plantillas, error de sync, en_límite)."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return [], "Conexión inválida", False

    connection, waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None:
        return [], "Conexión no encontrada", False

    sync_error = None
    if waba is not None and creds is not None and await acquire_sync_lock(tenant_id, waba.waba_id):
        try:
            await sync_templates(
                waba.waba_id,
                creds.access_token,
                creds.app_secret,
                creds.graph_api_version,
                connection.id,
                tenant_id,
            )
        except GraphError as exc:
            sync_error = exc.user_text[:300]
        except Exception:
            # El listado nunca se cae por el sync: se muestra lo guardado y el aviso.
            log.exception("templates_sync_al_listar_fallo", connection_id=str(connection.id))
            sync_error = "No se pudo sincronizar con Meta; se muestra lo último guardado."

    rows: list[MessageTemplate] = []
    if waba is not None:
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(MessageTemplate)
                        .where(
                            MessageTemplate.waba_account_id == waba.id,
                            MessageTemplate.tenant_id == tenant_id,
                        )
                        .order_by(MessageTemplate.name, MessageTemplate.language)
                    )
                )
                .scalars()
                .all()
            )

    at_limit = bool(
        waba is not None
        and waba.message_template_limit is not None
        and (waba.message_template_count or 0) >= waba.message_template_limit
    )
    items = [
        {
            "id": t.id,
            "name": t.name,
            "language": t.language,
            "category": t.category,
            "status": t.status,
            "quality_score": t.quality_score,
            "rejected_reason": t.rejected_reason,
        }
        for t in rows
    ]
    return items, sync_error, at_limit


async def sync_templates_now(tenant_id: uuid.UUID, connection_id: str) -> TemplateError | None:
    """Sincroniza ya, sin esperar el turno horario; a lo sumo una vez por minuto
    (`rate_limited`). Al terminar toma el candado horario para que el cron no repita."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates
    from core.memory.working import get_redis

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return TemplateError("invalid_connection", "Conexión inválida")

    connection, waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None or waba is None or creds is None:
        return TemplateError("not_ready", "No hay conexión/WABA lista todavía")
    redis = get_redis()
    key = _manual_sync_key(tenant_id, waba.waba_id)
    try:
        if not await redis.set(key, "1", ex=MANUAL_SYNC_RATE_LIMIT_S, nx=True):
            return TemplateError("rate_limited", MANUAL_SYNC_RATE_LIMITED)
    except Exception:
        # Redis caído: el límite es de cortesía (solo afecta al propio tenant), así que se
        # sigue sin él (fail-open) en vez de responder 500.
        log.warning("templates_sync_manual_rate_limit_sin_redis", connection_id=str(connection.id))

    async def _liberar_minuto() -> None:
        # Un sync fallido no debe consumir el minuto: el usuario puede reintentar ya.
        try:
            await redis.delete(key)
        except Exception:
            log.warning("templates_sync_manual_rate_limit_sin_redis", connection_id=str(connection.id))

    try:
        await sync_templates(
            waba.waba_id,
            creds.access_token,
            creds.app_secret,
            creds.graph_api_version,
            connection.id,
            tenant_id,
        )
    except GraphError as exc:
        await _liberar_minuto()
        return TemplateError("sync_failed", exc.user_text[:300])
    except Exception:
        log.exception("templates_sync_manual_fallo", connection_id=str(connection.id))
        await _liberar_minuto()
        return TemplateError("sync_failed", "No se pudo sincronizar con Meta. Intenta de nuevo en un momento.")
    # El cron y el sync automático del listado comparten este candado: ya está hecho esta hora.
    try:
        await acquire_sync_lock(tenant_id, waba.waba_id)
    except Exception:
        log.warning("templates_sync_manual_candado_sin_redis", connection_id=str(connection.id))
    return None


async def delete_template(
    tenant_id: uuid.UUID, connection_id: str, template_id: str
) -> TemplateError | None:
    """Borra todas las versiones de idioma de una plantilla.

    `not_deletable`: Meta respondió `(#100) Invalid parameter` al DELETE, que es lo que hace
    con sus plantillas de ejemplo; se explica y la fila sigue como estaba."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_templates import delete_template_all_languages

    try:
        cid = uuid.UUID(connection_id)
        tid_template = uuid.UUID(template_id)
    except ValueError:
        return TemplateError("invalid_template", "Plantilla inválida")

    connection, waba, creds = await resolve_waba_and_credentials(cid, tenant_id)
    if connection is None or waba is None or creds is None:
        return TemplateError("not_ready", "No hay conexión/WABA lista todavía")

    async with get_session() as session:
        row = await get_owned(session, MessageTemplate, tid_template, tenant_id)
    # La plantilla tiene que ser de la WABA de esta conexión: si no, el DELETE por nombre iría
    # a otra WABA del mismo negocio.
    if row is None or row.waba_account_id != waba.id:
        return TemplateError("not_found", "Plantilla no encontrada")

    try:
        await delete_template_all_languages(
            waba.waba_id,
            row.name,
            creds.access_token,
            creds.app_secret,
            creds.graph_api_version,
            connection.id,
            tenant_id,
            waba_account_id=waba.id,
        )
    except GraphError as exc:
        if exc.code == _GRAPH_INVALID_PARAMETER:
            return TemplateError("not_deletable", mensaje_no_eliminable(row.name))
        return TemplateError("delete_failed", exc.user_text[:300])
    except Exception:
        log.exception("template_delete_fallo", connection_id=str(connection.id), template_id=template_id)
        return TemplateError("delete_failed", "No se pudo eliminar la plantilla en Meta. Intenta de nuevo en un momento.")
    return None
