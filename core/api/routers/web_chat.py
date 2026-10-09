"""Chat web embebible: superficie pública (HU-CHAN-02).

`POST /api/v1/public/web-chat/{key}/messages` — un visitante envía un mensaje.
`GET  /api/v1/public/web-chat/{key}/messages?after=<iso|seq>` — el widget
recoge la respuesta del asistente por polling.
`GET  /api/v1/public/web-chat/{key}/config` — config no sensible para el widget.

Seguridad:
- La `key` es pública (va en el fragmento del widget) y resuelve el tenant.
- Solo se aceptan peticiones cuyo `Origin` esté en los dominios permitidos.
- Se exige un token de Turnstile si el tenant lo configuró.
- Rate limit por (widget, IP) en Redis, compartido entre instancias.
- El inbound se encola al MISMO stream que los demás canales: reutiliza el
  pipeline completo (kill-switch, cuotas, orquestador, guardrails).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import select, text

from core.db.models import Conversation, Customer, Message
from core.db.session import get_session
from core.gateway.webhooks import INBOUND_STREAM
from core.memory.working import get_redis
from core.security.login_lockout import client_ip
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)
router = APIRouter(prefix="/public/web-chat", tags=["public · web-chat"])

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Rate limit por (widget, IP) en Redis, compartido entre instancias. La clave
# lleva la `key` pública del widget (1:1 con el tenant) y no el `tenant_id`
# porque el límite corre ANTES de tocar la base: resolver el tenant primero
# costaría una query por request aun con el cupo agotado. Una key bien formada
# pero inexistente abre su propio cupo (y termina en 404). Ventana
# deslizante con un sorted set; el `EXPIRE` borra la clave de una IP que deja de
# mandar (sin crecer en memoria).
_RATE_LIMIT = 30          # mensajes por (widget, IP) por ventana
_RATE_WINDOW_S = 60
_RATE_KEY_PREFIX = "rl:web_chat:messages"
_WIDGET_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")  # `secrets.token_urlsafe`
_clock = time.time


def _rate_key(key: str, ip: str) -> str:
    # Una key que no puede ser de ningún widget no entra a la clave tal cual:
    # el path es input del visitante y no debe crear claves arbitrarias.
    # El marcador no cumple la regex: no comparte cupo con ninguna key real.
    key_part = key if _WIDGET_KEY_RE.fullmatch(key or "") else "!invalid"
    return f"{_RATE_KEY_PREFIX}:{key_part}:{ip}"


async def _rate_limited(key: str, ip: str) -> bool:
    """True si `ip` superó el cupo de este widget en la ventana.

    Los hits rechazados también cuentan (igual que antes). Si Redis no responde,
    deja pasar (fail-open): el `Origin` y Turnstile siguen siendo barreras, y el
    encolado del mensaje depende de Redis igual.
    """
    now = _clock()
    rate_key = _rate_key(key, ip)
    try:
        async with get_redis().pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(rate_key, "-inf", now - _RATE_WINDOW_S)
            pipe.zadd(rate_key, {f"{now:.6f}:{uuid.uuid4().hex}": now})
            # Tope de tamaño: alcanza con guardar los `_RATE_LIMIT + 1` hits más
            # recientes para decidir; una inundación no infla el set.
            pipe.zremrangebyrank(rate_key, 0, -(_RATE_LIMIT + 2))
            pipe.zcard(rate_key)
            pipe.expire(rate_key, _RATE_WINDOW_S)
            _, _, _, count, _ = await pipe.execute()
    except RedisError as exc:
        log.warning("web_chat_rate_limit_unavailable", error=str(exc))
        return False
    return int(count) > _RATE_LIMIT


def _norm(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def origin_allowed(origin: str, allowed: list[str]) -> bool:
    """True si `origin` está en la lista permitida. Comparación exacta de
    esquema+host (+puerto); se normaliza sin barra final."""
    if not origin:
        return False
    normalized = origin.rstrip("/").lower()
    return any(normalized == str(a).rstrip("/").lower() for a in allowed if a)


async def _verify_turnstile(token: str, secret: str, ip: str | None) -> bool:
    """Valida Turnstile (`siteverify`). Fail-closed."""
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            res = await client.post(
                TURNSTILE_VERIFY_URL,
                data={"secret": secret, "response": token, "remoteip": ip or ""},
            )
            data = res.json()
    except Exception:
        log.warning("turnstile_verify_error", exc_info=True)
        return False
    return bool(data.get("success"))


async def _resolve_by_key(key: str) -> tuple[uuid.UUID, dict[str, Any]] | None:
    """Resuelve (tenant_id, web_chat_config) por la clave pública del widget.

    `app_settings` tiene RLS forzada: la ruta pública no tiene tenant fijado, así
    que se usa la función `SECURITY DEFINER` `web_chat_resolve(text)` (migración
    0048), que devuelve solo esta fila y no permite enumerar.
    """
    if not key or len(key) > 128:
        return None
    async with get_session() as session:
        row = (
            await session.execute(
                text("select tenant_id, config from web_chat_resolve(:k)"), {"k": key}
            )
        ).first()
    if row is None:
        return None
    return row[0], dict(row[1] or {})


@router.get("/{key}/config")
async def web_chat_config(key: str) -> JSONResponse:
    """Config no sensible para el widget. Nunca expone el secreto de Turnstile."""
    found = await _resolve_by_key(key)
    if found is None:
        return JSONResponse({"error": "unknown_key"}, status_code=404)
    _tenant_id, config = found
    if not config.get("enabled"):
        return JSONResponse({"error": "disabled"}, status_code=403)
    return JSONResponse(
        {
            "enabled": True,
            "allowed_origins": list(config.get("allowed_origins", [])),
            "turnstile": bool(config.get("turnstile_secret")),
        }
    )


@router.post("/{key}/messages")
async def post_message(key: str, request: Request) -> JSONResponse:
    ip = client_ip(request)  # IP real detrás de proxies de confianza, no la del proxy
    if await _rate_limited(key, ip):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    found = await _resolve_by_key(key)
    if found is None:
        return JSONResponse({"error": "unknown_key"}, status_code=404)
    tenant_id, config = found
    if not config.get("enabled"):
        return JSONResponse({"error": "disabled"}, status_code=403)

    # Dominio permitido: se valida el `Origin` contra la allowlist del tenant.
    origin = request.headers.get("origin") or ""
    allowed = list(config.get("allowed_origins", []))
    if not origin_allowed(origin, allowed):
        log.warning("web_chat_origin_rejected", key=key, origin=origin)
        return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    content = _norm(body.get("content"))
    visitor_id = _norm(body.get("visitor_id"))
    turnstile_token = _norm(body.get("turnstile_token") or body.get("cf_turnstile_response"))
    if not content or len(content) > 2000 or not visitor_id or len(visitor_id) > 128:
        return JSONResponse({"error": "validation"}, status_code=422)

    turnstile_secret = _norm(config.get("turnstile_secret"))
    if turnstile_secret and not await _verify_turnstile(turnstile_token, turnstile_secret, ip):
        return JSONResponse({"error": "turnstile_failed"}, status_code=403)

    # Id de mensaje propio (no lo da un proveedor externo): uuid por envío.
    external_message_id = f"web:{uuid.uuid4()}"

    fields = {
        "tenant_id": str(tenant_id),
        "provider": "web",
        "channel": "web",
        "external_message_id": external_message_id,
        "customer_external_id": f"web:{visitor_id}",
        "customer_name": "",
        "content": content,
        "connection_external_id": key,
        "media": json.dumps([]),
        "media_context": "",
    }
    await get_redis().xadd(INBOUND_STREAM, fields)
    return JSONResponse({"ok": True, "message_id": external_message_id})


@router.get("/{key}/messages")
async def get_messages(key: str, request: Request, after: str | None = None) -> JSONResponse:
    """Devuelve los mensajes de la conversación del visitante posteriores a
    `after` (ISO-8601 de `created_at` del último recibido). El widget hace
    polling con este cursor."""
    found = await _resolve_by_key(key)
    if found is None:
        return JSONResponse({"error": "unknown_key"}, status_code=404)
    tenant_id, config = found
    if not config.get("enabled"):
        return JSONResponse({"error": "disabled"}, status_code=403)

    origin = request.headers.get("origin") or ""
    if not origin_allowed(origin, list(config.get("allowed_origins", []))):
        return JSONResponse({"error": "origin_not_allowed"}, status_code=403)

    visitor_id = _norm(request.query_params.get("visitor_id"))
    if not visitor_id or len(visitor_id) > 128:
        return JSONResponse({"error": "validation"}, status_code=422)

    since: datetime | None = None
    if after:
        try:
            since = datetime.fromisoformat(after)
        except ValueError:
            return JSONResponse({"error": "invalid_cursor"}, status_code=400)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            conversation = (
                await session.execute(
                    select(Conversation)
                    .join(Customer, Customer.id == Conversation.customer_id)
                    .where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.channel == "web",
                        Conversation.deleted_at.is_(None),
                        Customer.channel == "web",
                        Customer.external_id == f"web:{visitor_id}",
                        Customer.deleted_at.is_(None),
                    )
                    .order_by(Conversation.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conversation is None:
                return JSONResponse({"items": []})
            query = select(Message).where(
                Message.conversation_id == conversation.id,
                Message.tenant_id == tenant_id,
                Message.deleted_at.is_(None),
            )
            if since is not None:
                query = query.where(Message.created_at > since)
            rows = (
                await session.execute(query.order_by(Message.created_at).limit(50))
            ).scalars().all()

    items = [
        {
            "id": str(m.id),
            "direction": m.direction,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in rows
    ]
    return JSONResponse({"items": items})
