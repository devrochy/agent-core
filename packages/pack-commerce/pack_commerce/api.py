"""API pública del pack de comercio (ADR-0057): captación de leads desde landings.

Superficie pública `/api/pack/pack_commerce/…`, **autenticada por el pack** (sin
sesión de admin). Reemplaza el intake propuesto en ADR-0055 (que usaba
`/webhooks/*`, descartado por ADR-0057).

`POST /leads/{slug}`:

0. Rate limit por (tenant, IP) y de slugs inexistentes por IP, en Redis
   (`_rate_limited`): 429 `rate_limited`.
1. Resuelve el tenant por `slug` (`core.tenants.resolve_tenant_by_slug`).
2. Exige que el intake esté habilitado en la config del pack **de ese tenant**
   (`CRMConfig.lead_intake_enabled`).
3. Valida el header `X-Intake-Secret` contra `CRMConfig.lead_intake_secret`
   (comparación en tiempo constante).
4. Si el tenant configuró `CRMConfig.lead_intake_turnstile`, exige y valida el
   token de Cloudflare Turnstile (`siteverify`).
5. Crea/actualiza el lead de forma **idempotente**: `Customer` del core (identidad,
   ADR-0022) + `Lead` del pack (dominio comercial), con clave
   `intake:<email|phone>`.

Salida a red (ADR-0052): la validación de Turnstile no usa `httpx` directo, sino
`plugin_http(NETWORK)` (`NETWORK` es la `network_allowlist` del módulo).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.agents.spec import enabled_packs, load_active_spec
from core.db.models import Customer
from core.db.session import get_session
from core.gateway.webhook_verify import constant_time_equal
from core.mcp.types import plugin_http
from core.memory import entity as entity_memory
from core.plugin_manager import plugin_manager
from core.security import rate_limit
from core.security.login_lockout import client_ip
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tenants import resolve_tenant_by_slug
from pack_commerce.config import CRMConfig
from pack_commerce.models import Lead

log = get_logger(__name__)

PLUGIN_ID = "pack_commerce"
router = APIRouter(prefix="/api/pack/pack_commerce", tags=[PLUGIN_ID])

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# Hosts a los que el pack sale (solo el `siteverify` de Turnstile).
NETWORK = ("challenges.cloudflare.com",)
TURNSTILE_TIMEOUT = 8.0

# Rate limit en Redis (`core.security.rate_limit`), compartido entre instancias.
# Corre ANTES de tocar la base: resolver el tenant primero costaría una query
# por request aun con el cupo agotado. Dos cupos:
# - Por (slug, IP): la clave lleva el `slug` (1:1 con el tenant) y no el
#   `tenant_id`, que todavía no se conoce. Una IP no gasta el cupo de otro tenant.
# - Slugs inexistentes por IP: cada 404 suma un fallo; con el cupo lleno, esa IP
#   recibe 429 antes de resolver nada. Así un slug al azar por request (que abre
#   un cupo nuevo cada vez) no llega sin freno a la base. Solo cuentan los
#   fallos, y no frenan a los slugs que se sabe que existen (`_KNOWN_*`, se
#   anotan al resolver): tenants que comparten la IP de salida (hosting
#   compartido) no se cortan los leads entre sí, ni con tráfico legítimo ni
#   porque uno apunte a un slug que ya no existe.
# El intake es server→server; un abuso sostenido se corta en el WAF/proxy externo.
_RATE_LIMIT = 30
_RATE_WINDOW_S = 60
_RATE_KEY_PREFIX = "rl:pack_commerce:leads"
_MISS_LIMIT = 20
_MISS_WINDOW_S = 60
_MISS_KEY_PREFIX = "rl:pack_commerce:lead_misses"
_KNOWN_KEY_PREFIX = "rl:pack_commerce:lead_known"
_KNOWN_TTL_S = 900
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _rate_key(slug: str, ip: str) -> str:
    # Un slug que no puede ser de ningún tenant no entra a la clave tal cual:
    # el path es input del cliente y no debe crear claves arbitrarias.
    slug_part = slug if _SLUG_RE.fullmatch(slug or "") else "_invalid"
    return f"{_RATE_KEY_PREFIX}:{slug_part}:{ip}"


def _miss_bucket(ip: str) -> rate_limit.Bucket:
    return rate_limit.Bucket(f"{_MISS_KEY_PREFIX}:{ip}", _MISS_LIMIT, _MISS_WINDOW_S)


def _known_key(slug: str) -> str | None:
    return f"{_KNOWN_KEY_PREFIX}:{slug}" if _SLUG_RE.fullmatch(slug or "") else None


async def _rate_limited(slug: str, ip: str) -> bool:
    """True si `ip` superó el cupo de este tenant o, si no se sabe que el slug
    existe, el de slugs inexistentes.

    Si Redis no responde, deja pasar (fail-open): el secreto del intake sigue
    siendo la barrera real y perder leads por una caída de Redis sería peor.
    """
    bucket = rate_limit.Bucket(_rate_key(slug, ip), _RATE_LIMIT, _RATE_WINDOW_S)
    return await rate_limit.hit(
        bucket, guards=(_miss_bucket(ip),), known_key=_known_key(slug), name="lead_intake"
    )


def _norm(value: Any) -> str:
    return str(value).strip() if value is not None else ""


async def _verify_turnstile(token: str, secret: str, ip: str | None) -> bool:
    """Valida un token de Cloudflare Turnstile (`siteverify`). Fail-closed."""
    if not token:
        return False
    result = await plugin_http(NETWORK).request(
        "POST",
        TURNSTILE_VERIFY_URL,
        data={"secret": secret, "response": token, "remoteip": ip or ""},
        timeout=TURNSTILE_TIMEOUT,
    )
    if "error" in result:
        log.warning("turnstile_verify_error", error=result["error"])
        return False
    data = result["data"]
    return isinstance(data, dict) and bool(data.get("success"))


@router.get("/intake-config/{slug}")
async def intake_config(slug: str) -> JSONResponse:
    """Config **no sensible** del intake para el formulario alojado.

    La página embebible (`/f/<slug>`) la usa para aplicar el CSP `frame-ancestors`
    con los orígenes permitidos del tenant. No expone secretos; solo si el intake
    está activo, qué orígenes pueden incrustarlo y si exige Turnstile.
    """
    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        return JSONResponse({"error": "tenant_unknown"}, status_code=404)

    async with tenant_scope(tenant_id):
        spec = await load_active_spec(tenant_id)
        if PLUGIN_ID not in enabled_packs(spec):
            return JSONResponse({"error": "pack_disabled"}, status_code=403)

        config = await plugin_manager.load_config(PLUGIN_ID)
        if not isinstance(config, CRMConfig):
            return JSONResponse({"enabled": False, "allowed_origins": [], "turnstile": False})

        return JSONResponse(
            {
                "enabled": bool(config.lead_intake_enabled),
                "allowed_origins": list(config.lead_intake_allowed_origins),
                "turnstile": bool(config.lead_intake_turnstile.get_secret_value()),
            }
        )


@router.post("/leads/{slug}")
async def intake_lead(
    slug: str,
    request: Request,
    x_intake_secret: str | None = Header(default=None),
) -> JSONResponse:
    ip = client_ip(request)  # IP real detrás de proxies de confianza, no la del proxy
    if await _rate_limited(slug, ip):
        return JSONResponse({"error": "rate_limited"}, status_code=429)

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    nombre = _norm(body.get("nombre"))
    email = _norm(body.get("email"))
    phone = _norm(body.get("whatsapp"))
    negocio = _norm(body.get("negocio"))
    origen = _norm(body.get("origen"))
    optin = bool(body.get("optin"))
    turnstile_token = _norm(body.get("turnstile_token") or body.get("cf_turnstile_response"))

    if not nombre or not (email or phone):
        return JSONResponse({"error": "validation"}, status_code=422)

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        await rate_limit.record(_miss_bucket(ip), name="lead_intake")
        return JSONResponse({"error": "tenant_unknown"}, status_code=404)
    if known := _known_key(slug):
        await rate_limit.remember(known, _KNOWN_TTL_S, name="lead_intake")

    async with tenant_scope(tenant_id):
        spec = await load_active_spec(tenant_id)
        if PLUGIN_ID not in enabled_packs(spec):
            return JSONResponse({"error": "pack_disabled"}, status_code=403)

        config = await plugin_manager.load_config(PLUGIN_ID)
        if not isinstance(config, CRMConfig) or not config.lead_intake_enabled:
            return JSONResponse({"error": "intake_disabled"}, status_code=403)

        secret = config.lead_intake_secret.get_secret_value()
        if not secret:
            return JSONResponse({"error": "intake_not_configured"}, status_code=503)
        if not x_intake_secret or not constant_time_equal(x_intake_secret, secret):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        turnstile_secret = config.lead_intake_turnstile.get_secret_value()
        if turnstile_secret and not await _verify_turnstile(turnstile_token, turnstile_secret, ip):
            return JSONResponse({"error": "turnstile_failed"}, status_code=403)

        external_id = email or phone      # identidad del Customer (canal web)
        idem = f"intake:{external_id}"    # idempotencia del Lead (pack)

        async with get_session() as session:
            customer = (
                await session.execute(
                    select(Customer).where(
                        Customer.tenant_id == tenant_id,
                        Customer.channel == "web",
                        Customer.external_id == external_id,
                        Customer.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

            if customer is None:
                customer = Customer(
                    tenant_id=tenant_id,
                    channel="web",
                    external_id=external_id,
                    contact_phone=phone or None,
                    name=nombre,
                )
                session.add(customer)
                await session.flush()
                # El perfil es proyección de los hechos (ADR-0114 §3): se escribe por la memoria
                # del core, en esta misma transacción.
                await entity_memory.upsert_attributes_en_sesion(
                    session,
                    tenant_id,
                    customer.id,
                    {
                        "negocio": negocio,
                        "origen": origen,
                        "optin": optin,
                        "source": config.lead_intake_source,
                    },
                    source="import",
                )
            else:
                customer.name = nombre or customer.name
                customer.contact_phone = phone or customer.contact_phone

            lead = (
                await session.execute(
                    select(Lead).where(
                        Lead.tenant_id == tenant_id, Lead.idempotency_key == idem
                    )
                )
            ).scalar_one_or_none()

            if lead is None:
                session.add(
                    Lead(
                        customer_id=customer.id,
                        name=nombre,
                        phone=phone or None,
                        email=email or None,
                        interest=negocio or config.lead_intake_default_interest,
                        status=config.lead_intake_default_status,
                        idempotency_key=idem,
                    )
                )
            else:
                lead.name = nombre or lead.name
                lead.phone = phone or lead.phone
                lead.email = email or lead.email
                lead.interest = negocio or lead.interest

    log.info("lead_intake_ok", tenant_slug=slug)
    return JSONResponse({"ok": True})
