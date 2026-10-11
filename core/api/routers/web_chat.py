"""Chat web embebible: superficie pública (HU-CHAN-02).

`POST /api/v1/public/web-chat/{key}/messages` — un visitante envía un mensaje.
`GET  /api/v1/public/web-chat/{key}/messages?after=<iso|seq>` — el widget
recoge la respuesta del asistente por polling.
`GET  /api/v1/public/web-chat/{key}/config` — config no sensible para el widget.

Seguridad:
- La `key` es pública (va en el fragmento del widget) y resuelve el tenant.
- Solo se aceptan peticiones cuyo `Origin` esté en los dominios permitidos.
- Se exige un token de Turnstile si el tenant lo configuró.
- Rate limit por (widget, IP) y de keys inexistentes por IP, en Redis.
- El inbound se encola al MISMO stream que los demás canales: reutiliza el
  pipeline completo (kill-switch, cuotas, orquestador, guardrails).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select, text

from core.db.models import Conversation, Customer, Message
from core.db.session import get_session
from core.gateway.webhooks import INBOUND_STREAM
from core.memory.working import get_redis
from core.security import rate_limit
from core.security.login_lockout import client_ip
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)
router = APIRouter(prefix="/public/web-chat", tags=["public · web-chat"])

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Rate limit en Redis (`core.security.rate_limit`), compartido entre instancias.
# Corre ANTES de tocar la base: resolver el tenant primero costaría una query
# por request aun con el cupo agotado. Dos cupos:
# - Por (widget, IP): la clave lleva la `key` pública del widget (1:1 con el
#   tenant) y no el `tenant_id`, que todavía no se conoce. Una IP no gasta el
#   cupo de otro tenant.
# - Keys inexistentes por IP: cada 404 suma un fallo; con el cupo lleno, esa IP
#   recibe 429 antes de resolver nada. Así una key al azar por request (que abre
#   un cupo nuevo cada vez) no llega sin freno a la base. Solo cuentan los
#   fallos, y no frenan a las keys que se sabe que existen (`_KNOWN_*`, se
#   anotan al resolver): visitantes de distintos tenants detrás de la misma IP
#   (CGNAT de los operadores móviles) no se cortan el chat entre sí, ni con
#   tráfico legítimo ni porque uno mande keys al azar.
_RATE_LIMIT = 30          # mensajes por (widget, IP) por ventana
_RATE_WINDOW_S = 60
_RATE_KEY_PREFIX = "rl:web_chat:messages"
_MISS_LIMIT = 20          # keys inexistentes por IP por ventana
_MISS_WINDOW_S = 60
_MISS_KEY_PREFIX = "rl:web_chat:misses"
_KNOWN_KEY_PREFIX = "rl:web_chat:known"
_KNOWN_TTL_S = 900
_WIDGET_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")  # `secrets.token_urlsafe`


def _rate_key(key: str, ip: str) -> str:
    # Una key que no puede ser de ningún widget no entra a la clave tal cual:
    # el path es input del visitante y no debe crear claves arbitrarias.
    # El marcador no cumple la regex: no comparte cupo con ninguna key real.
    key_part = key if _WIDGET_KEY_RE.fullmatch(key or "") else "!invalid"
    return f"{_RATE_KEY_PREFIX}:{key_part}:{ip}"


def _miss_bucket(ip: str) -> rate_limit.Bucket:
    return rate_limit.Bucket(f"{_MISS_KEY_PREFIX}:{ip}", _MISS_LIMIT, _MISS_WINDOW_S)


def _known_key(key: str) -> str | None:
    return f"{_KNOWN_KEY_PREFIX}:{key}" if _WIDGET_KEY_RE.fullmatch(key or "") else None


async def _rate_limited(key: str, ip: str) -> bool:
    """True si `ip` superó el cupo de este widget o, si no se sabe que la key
    existe, el de keys inexistentes.

    Si Redis no responde, deja pasar (fail-open): el `Origin` y Turnstile siguen
    siendo barreras, y el encolado del mensaje depende de Redis igual.
    """
    bucket = rate_limit.Bucket(_rate_key(key, ip), _RATE_LIMIT, _RATE_WINDOW_S)
    return await rate_limit.hit(
        bucket, guards=(_miss_bucket(ip),), known_key=_known_key(key), name="web_chat"
    )


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
    """Resuelve (tenant_id, config) por la clave pública del widget.

    `channel_connections` y `app_settings` tienen RLS forzada y la ruta pública no tiene tenant
    fijado: se resuelve con funciones `SECURITY DEFINER` que no permiten enumerar.

    1. La conexión `webchat` (ADR-0115 §4): `webchat_resolve(text)` (0086) devuelve solo lo no
       secreto (negocio, conexión, habilitada, dominios y si hay secreto de Turnstile). `config`
       lleva `connection_id` y `turnstile` (bool); el secreto lo lee el POST con RLS
       (`_secreto_turnstile`).
    2. Si el negocio nunca tuvo una conexión `webchat` (antes del backfill), `web_chat_resolve`
       (0048) sobre `app_settings`, como antes; `config` trae ahí el secreto. Se retira en el
       *contract*.
    """
    if not key or len(key) > 128:
        return None
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "select tenant_id, connection_id, active, allowed_origins, has_secret from webchat_resolve(:k)"
                ),
                {"k": key},
            )
        ).first()
    if fila is not None:
        origenes = fila[3]
        return fila[0], {
            "enabled": bool(fila[2]),
            "allowed_origins": [str(o) for o in origenes if o] if isinstance(origenes, list) else [],
            "turnstile": bool(fila[4]),
            "connection_id": fila[1],
        }

    async with get_session() as session:
        row = (
            await session.execute(
                text("select tenant_id, config from web_chat_resolve(:k)"), {"k": key}
            )
        ).first()
    if row is None:
        return None
    tenant_id, config = row[0], dict(row[1] or {})
    from core.gateway import chat_web_conexion

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await chat_web_conexion.tiene_alguna(session, tenant_id):
                # El negocio ya tiene (o tuvo) su conexión: la clave de `app_settings` que no está
                # en ella es vieja (o el widget se borró) y no revive.
                return None
    return tenant_id, config


async def _secreto_turnstile(tenant_id: uuid.UUID, config: dict[str, Any]) -> str:
    """El secreto de Turnstile: el de `app_settings` (camino anterior) o, si el widget es una
    conexión con secreto, el suyo, descifrado dentro del `tenant_scope` del negocio."""
    legacy = _norm(config.get("turnstile_secret"))
    connection_id = config.get("connection_id")
    if legacy or connection_id is None or not config.get("turnstile"):
        return legacy
    from core.gateway import chat_web_conexion

    try:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                return await chat_web_conexion.secreto_turnstile(session, tenant_id, connection_id)
    except Exception:  # p. ej. el secreto no se descifra (rotación de la clave maestra)
        # Sin detalle: el llamador responde 403 (falla cerrado).
        log.error("web_chat_secreto_ilegible", connection_id=str(connection_id))
        return ""


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
            "turnstile": bool(config.get("turnstile") or config.get("turnstile_secret")),
        }
    )


@router.post("/{key}/messages")
async def post_message(key: str, request: Request) -> JSONResponse:
    ip = client_ip(request)  # IP real detrás de proxies de confianza, no la del proxy
    if await _rate_limited(key, ip):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    found = await _resolve_by_key(key)
    if found is None:
        await rate_limit.record(_miss_bucket(ip), name="web_chat")
        return JSONResponse({"error": "unknown_key"}, status_code=404)
    if known := _known_key(key):
        await rate_limit.remember(known, _KNOWN_TTL_S, name="web_chat")
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

    turnstile_secret = await _secreto_turnstile(tenant_id, config)
    if config.get("turnstile") and not turnstile_secret:
        # La conexión dice tener secreto pero no se pudo leer: falla cerrado.
        return JSONResponse({"error": "turnstile_failed"}, status_code=403)
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

    query = (
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
    )
    connection_id = config.get("connection_id")
    if connection_id is not None:
        # Chat web como conexión (ADR-0115 §4): solo la conversación de ESTE widget. Con dos
        # widgets del negocio, un visitante no ve la del otro. Las de antes del backfill (sin
        # conexión) son del mismo visitante y negocio: se siguen viendo.
        query = query.where(
            or_(Conversation.connection_id == connection_id, Conversation.connection_id.is_(None))
        )
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            conversation = (
                await session.execute(query.order_by(Conversation.started_at.desc()).limit(1))
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
