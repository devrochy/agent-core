"""Checklist de promoción a producción, en dos partes (HU-CONN-07):

- **Por conexión** (`run_connection_production_checks`): número registrado, WABA suscrita,
  webhook, plantillas mínimas aprobadas y token, evaluados con las credenciales de ESA
  conexión. Lo ve el negocio en "Producción" de cada conexión.
- **Plataforma** (`run_platform_checks`): la app de Meta de la plataforma y la
  infraestructura. Solo el master, en "Plataforma → App de Meta".

Las dos corren como job de Celery (`core/tasks/production_readiness.py`) y el resultado queda
guardado con fecha (`core/admin/production_readiness_service.py`): nunca en el request ni con
pytest. Cada check devuelve `PreflightCheck` (reusado de `meta_preflight.py`, mismo patrón que
`meta_health.py`).

IMPORTANTE: la mayoría de los checks de plataforma solo dan una respuesta con sentido
corriendo contra un despliegue de PRODUCCIÓN real -- no contra el Postgres/
Redis efímero de CI. Cada función documenta explícitamente si depende de
un entorno real o corre igual en cualquier lado. Ver
`scripts/verify_production.py` para el runner standalone y
`docs/despliegue-coolify.md` para el runbook de promoción.

Traducciones explícitas de conceptos Supabase/Next.js que no existen en
este stack (ver docs/adr para el ADR completo):
- `service_role` key / "exposed schemas" (PostgREST) -> no hay Supabase.
  El análogo real es RLS con FORCE (migración 0029) + grants de Postgres
  sobre el schema `meta_private`.
- pgTap -> los tests pytest de aislamiento multi-tenant corren en CI; el check
  `rls_tests_pass` solo informa el commit desplegado (no corre tests en el servidor).
- `NEXT_PUBLIC_*` -> no hay bundler ni env vars expuestas al cliente en
  este panel Jinja2 server-rendered. Se reinterpreta como: ningún template
  interpola un campo cuyo nombre contenga SECRET/SERVICE_ROLE/TOKEN.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from core.config import get_settings
from core.gateway.meta_preflight import PreflightCheck
from core.shared.logger import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
_HEARTBEAT_KEY = "worker:heartbeat"
_HEARTBEAT_MAX_AGE_S = 180


async def check_webhook_handshake(callback_url: str, verify_token: str) -> PreflightCheck:
    """VIVE DE PRODUCCIÓN REAL: hace un GET real por red contra la URL
    pública -- no llama a la función del handler local, porque el punto es
    verificar lo que Meta vería de verdad (DNS, TLS, proxy/middleware). Una URL
    sin HTTPS falla sin llamar: Meta no la acepta."""
    now = datetime.now(UTC)
    if not callback_url.startswith("https://"):
        # Meta no registra un webhook sin HTTPS: aunque responda, no sirve (HU-SH-05).
        return PreflightCheck(
            key="webhook_handshake", ok=False,
            detail="PUBLIC_BASE_URL no es HTTPS: Meta no acepta el webhook.", checked_at=now,
        )
    challenge = uuid.uuid4().hex
    params = {"hub.mode": "subscribe", "hub.verify_token": verify_token, "hub.challenge": challenge}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(callback_url, params=params)
    except httpx.HTTPError as exc:
        return PreflightCheck(key="webhook_handshake", ok=False, detail=f"No se pudo contactar {callback_url} ({type(exc).__name__})", checked_at=now)

    content_type = response.headers.get("content-type", "")
    if response.status_code == 200 and response.text == challenge and content_type.startswith("text/plain"):
        return PreflightCheck(key="webhook_handshake", ok=True, detail="Handshake correcto", checked_at=now)
    return PreflightCheck(
        key="webhook_handshake", ok=False,
        detail=f"status={response.status_code} content-type={content_type!r} body={response.text[:100]!r}",
        checked_at=now,
    )


# Objetos de suscripción de app que exige el paso a producción (HU-CONN-07). Hoy la plataforma
# solo opera WhatsApp: `page` (Messenger) e `instagram` no son obligatorios y, si faltan, se
# informan sin poner el check en rojo. Un canal nuevo se agrega aquí cuando entre en producción.
REQUIRED_APP_OBJECTS: tuple[str, ...] = ("whatsapp_business_account",)


async def check_app_subscriptions_complete(
    meta_app_id: str, required_objects: tuple[str, ...] = REQUIRED_APP_OBJECTS
) -> PreflightCheck:
    """Lee el estado ya persistido por el cron de 15 min
    (`meta_subscriptions_health.py`) -- no repite el GET, mismo criterio
    que `meta_health.py::check_app_subscription`.

    Solo los objetos de `required_objects` deciden el resultado (HU-CONN-07): antes se exigían
    también `page` e `instagram`, y una app que solo usa WhatsApp nunca pasaba. Los opcionales
    sin suscripción se nombran en el detalle."""
    from core.gateway.meta_subscriptions import OBJECT_FIELDS, list_app_subscriptions

    now = datetime.now(UTC)
    if not meta_app_id:
        return PreflightCheck(
            key="app_subscriptions_complete", ok=False, detail="META_APP_ID no configurado", checked_at=now
        )
    statuses = await list_app_subscriptions(meta_app_id)
    by_object = {s.object: s for s in statuses}

    missing = []
    optional_missing = []
    for object_, min_fields in OBJECT_FIELDS.items():
        row = by_object.get(object_)
        if object_ not in required_objects:
            if row is None or row.active is not True:
                optional_missing.append(object_)
            continue
        if row is None or row.active is not True:
            missing.append(f"{object_} (active={row.active if row else 'sin suscripción'})")
            continue
        missing_fields = set(min_fields) - set(row.fields)
        if missing_fields:
            missing.append(f"{object_} (faltan fields: {sorted(missing_fields)})")

    optional = f" No obligatorios sin suscripción: {', '.join(optional_missing)}." if optional_missing else ""
    if missing:
        return PreflightCheck(
            key="app_subscriptions_complete", ok=False,
            detail=f"Problemas: {'; '.join(missing)}.{optional}", checked_at=now,
        )
    return PreflightCheck(
        key="app_subscriptions_complete", ok=True,
        detail=f"Activos con fields completos: {', '.join(required_objects)}.{optional}", checked_at=now,
    )


def check_meta_app_configured() -> PreflightCheck:
    """Las variables de la app de Meta de la plataforma están en el entorno del servidor:
    `META_APP_ID` y `META_APP_SECRET` (webhook, suscripciones, Embedded Signup) y
    `META_CONFIG_ID` (la configuración de Embedded Signup, HU-SH-05). No muestra valores."""
    now = datetime.now(UTC)
    settings = get_settings()
    faltan = [
        nombre
        for nombre, valor in (
            ("META_APP_ID", settings.meta_app_id),
            ("META_APP_SECRET", settings.meta_app_secret),
            ("META_CONFIG_ID", settings.meta_config_id),
        )
        if not valor
    ]
    if faltan:
        return PreflightCheck(
            key="meta_app_configured", ok=False,
            detail=f"Faltan en el entorno del servidor: {', '.join(faltan)}.", checked_at=now,
        )
    return PreflightCheck(
        key="meta_app_configured", ok=True, detail="META_APP_ID, META_APP_SECRET y META_CONFIG_ID configurados.",
        checked_at=now,
    )


def check_prod_secret_differs_from_dev(current_secret: str) -> PreflightCheck:
    """Nunca compara valores en texto plano -- solo hashes. `ok=None` si
    `DEV_APP_SECRET_HASH` no está configurado (nunca `True` por default)."""
    now = datetime.now(UTC)
    dev_hash = get_settings().dev_app_secret_hash
    if not dev_hash:
        return PreflightCheck(
            key="prod_secret_differs_from_dev", ok=None,
            detail="DEV_APP_SECRET_HASH no configurado -- no se puede comparar", checked_at=now,
        )
    current_hash = hashlib.sha256(current_secret.encode()).hexdigest()
    if current_hash == dev_hash:
        return PreflightCheck(
            key="prod_secret_differs_from_dev", ok=False,
            detail="El App Secret de producción coincide con el de desarrollo", checked_at=now,
        )
    return PreflightCheck(key="prod_secret_differs_from_dev", ok=True, detail="Distinto del de desarrollo", checked_at=now)


def check_token_keys_configured() -> PreflightCheck:
    """Formato exacto: TOKEN_MASTER_KEYS es JSON {key_id: clave_base64},
    TOKEN_ACTIVE_KEY_ID debe estar entre las claves, y la clave activa debe
    decodificar a exactamente 32 bytes (AES-256-GCM)."""
    now = datetime.now(UTC)
    settings = get_settings()
    try:
        raw = json.loads(settings.token_master_keys or "{}")
    except json.JSONDecodeError:
        return PreflightCheck(key="token_keys_configured", ok=False, detail="TOKEN_MASTER_KEYS no es JSON válido", checked_at=now)
    if not isinstance(raw, dict) or not raw:
        return PreflightCheck(key="token_keys_configured", ok=False, detail="TOKEN_MASTER_KEYS vacío o no es un objeto", checked_at=now)

    active_key_id = settings.token_active_key_id
    if not active_key_id:
        return PreflightCheck(key="token_keys_configured", ok=False, detail="TOKEN_ACTIVE_KEY_ID no configurado", checked_at=now)
    if active_key_id not in raw:
        return PreflightCheck(
            key="token_keys_configured", ok=False,
            detail=f"TOKEN_ACTIVE_KEY_ID={active_key_id!r} no está en TOKEN_MASTER_KEYS", checked_at=now,
        )

    try:
        decoded = base64.b64decode(raw[active_key_id])
    except Exception:
        return PreflightCheck(key="token_keys_configured", ok=False, detail=f"La clave activa {active_key_id!r} no es base64 válido", checked_at=now)
    if len(decoded) != 32:
        return PreflightCheck(
            key="token_keys_configured", ok=False,
            detail=f"La clave activa {active_key_id!r} tiene {len(decoded)} bytes, se esperan 32", checked_at=now,
        )
    return PreflightCheck(key="token_keys_configured", ok=True, detail=f"Clave activa {active_key_id!r} de 32 bytes", checked_at=now)


async def check_worker_heartbeat_and_dlq() -> PreflightCheck:
    """VIVE DE PRODUCCIÓN REAL: el heartbeat lo escribe el cron
    `core.worker_heartbeat` corriendo en el worker de Celery real; contra
    un Redis vacío (CI) siempre va a dar `ok=False` por ausencia -- es el
    comportamiento correcto, no un bug del check."""
    from core.memory.working import get_redis
    from core.orchestrator.consumer import dlq_pendientes

    now = datetime.now(UTC)
    redis = get_redis()

    raw_heartbeat = await redis.get(_HEARTBEAT_KEY)
    if not raw_heartbeat:
        return PreflightCheck(key="worker_heartbeat_and_dlq", ok=False, detail="Sin heartbeat de worker en Redis", checked_at=now)
    try:
        heartbeat_at = datetime.fromisoformat(raw_heartbeat if isinstance(raw_heartbeat, str) else raw_heartbeat.decode())
    except (TypeError, ValueError):
        return PreflightCheck(key="worker_heartbeat_and_dlq", ok=False, detail="Heartbeat con formato inválido", checked_at=now)
    if now - heartbeat_at > timedelta(seconds=_HEARTBEAT_MAX_AGE_S):
        return PreflightCheck(
            key="worker_heartbeat_and_dlq", ok=False,
            detail=f"Heartbeat viejo ({heartbeat_at.isoformat()}) -- el worker puede estar caído", checked_at=now,
        )

    dlq_size = await dlq_pendientes(redis)
    if dlq_size > 0:
        return PreflightCheck(key="worker_heartbeat_and_dlq", ok=False, detail=f"DLQ con {dlq_size} eventos pendientes", checked_at=now)

    return PreflightCheck(key="worker_heartbeat_and_dlq", ok=True, detail="Worker vivo, DLQ vacío", checked_at=now)


async def check_rls_forced() -> PreflightCheck:
    """Corre contra el `DATABASE_URL` actual -- en CI corre contra el
    Postgres efímero (con sentido: confirma que las migraciones dejaron el
    esquema bien), en producción contra la BD real."""
    from sqlalchemy import text

    from core.db.session import get_session

    now = datetime.now(UTC)
    async with get_session() as session:
        unforced = (
            await session.execute(
                text(
                    # `pg_tables` no tiene `forcerowsecurity` -- hay que ir a
                    # `pg_class`/`pg_namespace` directo.
                    "select c.relname from pg_class c "
                    "join pg_namespace n on n.oid = c.relnamespace "
                    "where n.nspname = 'public' and c.relkind = 'r' "
                    "and c.relrowsecurity = true and not c.relforcerowsecurity"
                )
            )
        ).scalars().all()
        public_has_usage = (
            await session.execute(
                text("select has_schema_privilege('public', 'meta_private', 'USAGE')")
            )
        ).scalar()

    problems = []
    if unforced:
        problems.append(f"sin FORCE ROW LEVEL SECURITY: {sorted(unforced)}")
    if public_has_usage:
        problems.append("schema meta_private tiene USAGE otorgado a PUBLIC")

    if problems:
        return PreflightCheck(key="rls_forced", ok=False, detail="; ".join(problems), checked_at=now)
    return PreflightCheck(key="rls_forced", ok=True, detail="RLS forzada en todo public, meta_private sin grants a PUBLIC", checked_at=now)


def check_rls_tests_pass() -> PreflightCheck:
    """Informa dónde se verifican los tests de aislamiento multi-tenant: en CI.

    Antes corría `pytest tests/integration` en el proceso, pero la imagen desplegada no
    trae pytest ni `tests/` (siempre daba exit=1) y, si los trajera, esos tests crean
    negocios en la base configurada: en un entorno real ensuciarían datos. El CI los
    corre con su propia base en cada PR (job build-and-test). `ok=None` = no concluyente
    desde aquí; el aislamiento en runtime lo cubre `rls_forced`.
    """
    import os

    now = datetime.now(UTC)
    commit = (os.environ.get("SOURCE_COMMIT") or "")[:7]
    rama = os.environ.get("COOLIFY_BRANCH") or ""
    desplegado = f"commit desplegado {commit}" + (f" ({rama})" if rama else "") if commit else "commit desplegado desconocido"
    return PreflightCheck(
        key="rls_tests_pass",
        ok=None,
        detail=f"Los tests de aislamiento multi-tenant corren en CI (build-and-test), no en el servidor; {desplegado}.",
        checked_at=now,
    )


def _looks_like_secret_field(dotted_name: str) -> bool:
    """Compara por COMPONENTE (separado por `.`/`_`), no por substring --
    un substring ingenuo de "TOKEN" atrapa falsos positivos reales de este
    mismo panel (`row.tokens_in`/`row.tokens_out`, conteo de tokens de LLM,
    nada que ver con un secreto)."""
    parts = re.split(r"[._]", dotted_name.lower())
    if "secret" in parts or "token" in parts:
        return True
    return "service" in parts and "role" in parts


def check_no_secret_leak_in_templates() -> PreflightCheck:
    """No hay bundler ni convención NEXT_PUBLIC_ en este panel -- se
    reinterpreta como: ningún template/JS estático interpola un campo
    cuyo nombre sugiera un secreto."""
    now = datetime.now(UTC)
    offenders = []
    for pattern in ("core/admin/templates/*.html", "core/admin/static/*.js"):
        for path in ROOT.glob(pattern):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}", text):
                if _looks_like_secret_field(match.group(1)):
                    offenders.append(f"{path.relative_to(ROOT)}: {{{{ {match.group(1)} }}}}")

    if offenders:
        return PreflightCheck(key="no_secret_leak_in_templates", ok=False, detail="; ".join(offenders), checked_at=now)
    return PreflightCheck(key="no_secret_leak_in_templates", ok=True, detail="Sin interpolaciones sospechosas", checked_at=now)


async def check_connections_health_except_billing(tenant_id: uuid.UUID) -> PreflightCheck:
    """Reusa `meta_health.py::run_health_checks` tal cual -- excluye
    `billing` (no automatizable, `ok=None` siempre) antes de exigir que
    todo lo demás sea `True`."""
    from sqlalchemy import select

    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway.meta_health import run_health_checks

    now = datetime.now(UTC)
    async with get_session() as session:
        # Solo las que están en uso: una desactivada (`revoked`), sin alta (`draft`) o borrada no
        # traba el paso a producción (desde ADR-0102, una BYOK sin credenciales falla el token).
        connections = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.status.not_in(("revoked", "draft")),
                    MetaConnection.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    if not connections:
        return PreflightCheck(key="connections_health", ok=None, detail="Sin conexiones para este tenant", checked_at=now)

    failing = []
    for connection in connections:
        checks = await run_health_checks(connection, tenant_id)
        for key, check in checks.items():
            if key == "billing":
                continue
            if check.get("ok") is False:
                failing.append(f"{connection.channel}:{key}")

    if failing:
        return PreflightCheck(key="connections_health", ok=False, detail=f"Checks fallando: {', '.join(failing)}", checked_at=now)
    return PreflightCheck(key="connections_health", ok=True, detail="Todas las conexiones sanas (salvo billing, manual)", checked_at=now)


# ------------------------------------------------------------------ plataforma (HU-CONN-07)
# Checks de la app de Meta de la PLATAFORMA y de la infraestructura: los ve solo el master
# ("Plataforma → App de Meta") y corren en el worker de Celery, nunca en el request.
PLATFORM_CHECK_LABELS: dict[str, str] = {
    "meta_app_configured": "Variables de la app de Meta (META_APP_ID, META_APP_SECRET, META_CONFIG_ID)",
    "webhook_handshake": "El webhook de la plataforma (HTTPS) responde la verificación de Meta",
    "app_subscriptions_complete": "Suscripciones de la app de Meta (WhatsApp)",
    "prod_secret_differs_from_dev": "App Secret distinto al de desarrollo",
    "token_keys_configured": "Claves de cifrado de tokens",
    "worker_heartbeat_and_dlq": "Worker vivo y bandeja de fallos vacía",
    "rls_forced": "Aislamiento entre negocios forzado en la base",
    "rls_tests_pass": "Tests de aislamiento (corren en CI)",
    "no_secret_leak_in_templates": "Sin secretos en las plantillas del panel",
}


async def safe_check(key: str, check: Callable[[], Awaitable[PreflightCheck] | PreflightCheck]) -> PreflightCheck:
    """Un check que revienta queda en `None` con el tipo de error: no tumba la corrida."""
    try:
        result = check()
        if isinstance(result, PreflightCheck):
            return result
        return await result
    except Exception as exc:
        log.exception("readiness_check_failed", check=key)
        return PreflightCheck(
            key=key, ok=None, detail=f"No se pudo evaluar ({type(exc).__name__})", checked_at=datetime.now(UTC)
        )


async def run_platform_checks() -> list[PreflightCheck]:
    """Los checks de la app de la plataforma e infraestructura, en orden. Sin pytest ni
    subprocesos: `rls_tests_pass` solo informa el commit desplegado."""
    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"
    return [
        await safe_check("meta_app_configured", check_meta_app_configured),
        await safe_check("webhook_handshake", lambda: check_webhook_handshake(callback_url, settings.whatsapp_verify_token)),
        await safe_check("app_subscriptions_complete", lambda: check_app_subscriptions_complete(settings.meta_app_id)),
        await safe_check("prod_secret_differs_from_dev", lambda: check_prod_secret_differs_from_dev(settings.meta_app_secret)),
        await safe_check("token_keys_configured", check_token_keys_configured),
        await safe_check("worker_heartbeat_and_dlq", check_worker_heartbeat_and_dlq),
        await safe_check("rls_forced", check_rls_forced),
        await safe_check("rls_tests_pass", check_rls_tests_pass),
        await safe_check("no_secret_leak_in_templates", check_no_secret_leak_in_templates),
    ]


# ------------------------------------------------------------- por conexión (HU-CONN-07)
# Lo que necesita UNA conexión para operar en producción, evaluado con SUS credenciales
# (resolver de HU-CONN-01). Para Messenger e Instagram solo aplican token y webhook.
CONNECTION_ITEM_LABELS: dict[str, str] = {
    "token": "Token de acceso",
    "number_registered": "Número registrado en la Cloud API",
    "waba_subscribed": "WABA suscrita a la app",
    "webhook": "Webhook",
    "templates_approved": "Plantillas mínimas aprobadas",
}
_SOLO_WHATSAPP = frozenset({"number_registered", "waba_subscribed", "templates_approved"})


def connection_item_keys(channel: str) -> list[str]:
    return [key for key in CONNECTION_ITEM_LABELS if channel == "whatsapp" or key not in _SOLO_WHATSAPP]


def combine_checks(key: str, checks: list[PreflightCheck]) -> PreflightCheck:
    """Un ítem del checklist a partir de varios checks: `False` si alguno falla, `None` si
    alguno no se pudo evaluar, `True` si todos pasan. El detalle es el de los que deciden."""
    now = datetime.now(UTC)
    if not checks:
        return PreflightCheck(key=key, ok=None, detail="Sin datos para evaluar", checked_at=now)
    failing = [c for c in checks if c.ok is False]
    unknown = [c for c in checks if c.ok is None]
    deciding = failing or unknown or checks
    ok: bool | None = False if failing else (None if unknown else True)
    return PreflightCheck(
        key=key,
        ok=ok,
        detail=" · ".join(dict.fromkeys(c.detail for c in deciding if c.detail)),
        checked_at=max((c.checked_at for c in checks if c.checked_at), default=now),
        repair_action=next((c.repair_action for c in deciding if c.repair_action), None),
    )


def _with_number(check: PreflightCheck, phone: str | None) -> PreflightCheck:
    if not phone:
        return check
    return check.model_copy(update={"detail": f"{phone}: {check.detail}"})


async def check_minimum_templates(
    connection_id: uuid.UUID, tenant_id: uuid.UUID, waba_account_ids: list[uuid.UUID]
) -> PreflightCheck:
    """Plantillas mínimas aprobadas en la WABA de ESTA conexión (no pega a Meta).

    Mínimo = al menos una aprobada (fuera de la ventana de 24 h solo se escribe con plantilla)
    y aprobadas todas las que el negocio asignó a sus avisos (`message_templates_map`,
    HU-ARCH-13): un aviso con una plantilla sin aprobar no sale."""
    from sqlalchemy import select

    from core.db.models import MessageTemplate
    from core.db.session import get_session
    from core.gateway.event_notices import load_templates_map

    now = datetime.now(UTC)
    if not waba_account_ids:
        return PreflightCheck(
            key="templates_approved", ok=False, detail="La conexión no tiene WABA: no hay plantillas.",
            checked_at=now, repair_action="reauthorize",
        )
    async with get_session() as session:
        rows = (
            await session.execute(
                select(MessageTemplate.name, MessageTemplate.language).where(
                    MessageTemplate.tenant_id == tenant_id,
                    MessageTemplate.waba_account_id.in_(waba_account_ids),
                    MessageTemplate.status == "APPROVED",
                )
            )
        ).all()
    approved = {(name, language) for name, language in rows}
    mapping = await load_templates_map(tenant_id)
    required = {
        (str(entry["template"]), str(entry.get("language") or "es"))
        for entry in mapping.values()
        if isinstance(entry, dict) and entry.get("template")
    }
    missing = sorted(required - approved)
    if missing:
        nombres = ", ".join(f"{name} ({language})" for name, language in missing)
        return PreflightCheck(
            key="templates_approved", ok=False,
            detail=f"Avisos con plantillas sin aprobar en esta conexión: {nombres}.",
            checked_at=now, repair_action="manage_templates",
        )
    if not approved:
        return PreflightCheck(
            key="templates_approved", ok=False,
            detail="No hay plantillas aprobadas: fuera de la ventana de 24 h no se puede escribir.",
            checked_at=now, repair_action="manage_templates",
        )
    avisos = f"; las {len(required)} de los avisos están aprobadas" if required else ""
    cuantas = "1 plantilla aprobada" if len(approved) == 1 else f"{len(approved)} plantillas aprobadas"
    return PreflightCheck(key="templates_approved", ok=True, detail=f"{cuantas}{avisos}.", checked_at=now)


async def run_connection_production_checks(connection: Any, tenant_id: uuid.UUID) -> list[PreflightCheck]:
    """Checklist de producción de UNA conexión, con las credenciales de ESA conexión.

    Exige un `tenant_scope` activo del negocio dueño (lo pide `credentials_for`). Sin
    credenciales utilizables el token falla y lo que depende de Meta queda sin evaluar."""
    from sqlalchemy import select

    from core.db.models import PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_health
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for

    async with get_session() as session:
        wabas = (
            await session.execute(
                select(WabaAccount)
                .where(
                    WabaAccount.connection_id == connection.id,
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.deleted_at.is_(None),  # ADR-0122: borrado lógico con su conexión
                )
                .order_by(WabaAccount.created_at)
            )
        ).scalars().all()
        phones = (
            await session.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.connection_id == connection.id,
                    PhoneNumber.tenant_id == tenant_id,
                    PhoneNumber.deleted_at.is_(None),
                )
                .order_by(PhoneNumber.created_at)
            )
        ).scalars().all()
    waba = wabas[0] if wabas else None
    keys = connection_item_keys(connection.channel)
    now = datetime.now(UTC)

    creds = None
    sin_credenciales = ""
    try:
        creds = await credentials_for(connection, waba_id=waba.waba_id if waba is not None else None)
    except CredentialsUnavailable as exc:
        sin_credenciales = exc.message

    def _sin_credenciales(key: str) -> PreflightCheck:
        return PreflightCheck(
            key=key, ok=None, detail="No se pudo revisar: la conexión no tiene credenciales utilizables.",
            checked_at=now, repair_action="reauthorize",
        )

    items: dict[str, PreflightCheck] = {}

    # Token: sin credenciales no hay paso a producción (siempre rojo, no "sin dato").
    if creds is None:
        items["token"] = PreflightCheck(
            key="token", ok=False, detail=f"Sin credenciales: {sin_credenciales}", checked_at=now,
            repair_action="reauthorize",
        )
    else:
        token = await safe_check("token", lambda: meta_health.check_token_valid(
            creds.access_token, creds.app_id, creds.app_secret, creds.graph_api_version,
            connection_id=connection.id, tenant_id=tenant_id,
        ))
        items["token"] = combine_checks("token", [token])

    if "number_registered" in keys:
        if not phones:
            items["number_registered"] = PreflightCheck(
                key="number_registered", ok=False, detail="La conexión no tiene números.", checked_at=now,
                repair_action="add_number",
            )
        elif creds is None:
            items["number_registered"] = _sin_credenciales("number_registered")
        else:
            checks = []
            for phone in phones:
                check = await safe_check("number_registered", lambda phone=phone: meta_health.check_number_registered(
                    phone.phone_number_id, creds.access_token, creds.app_secret, creds.graph_api_version,
                    connection.id, tenant_id=tenant_id,
                ))
                checks.append(_with_number(check, phone.display_phone_number if len(phones) > 1 else None))
            items["number_registered"] = combine_checks("number_registered", checks)

    if "waba_subscribed" in keys:
        if waba is None:
            items["waba_subscribed"] = PreflightCheck(
                key="waba_subscribed", ok=False, detail="La conexión no tiene la WABA cargada.", checked_at=now,
                repair_action="reauthorize",
            )
        elif creds is None:
            items["waba_subscribed"] = _sin_credenciales("waba_subscribed")
        else:
            checks = [
                await safe_check("waba_subscribed", lambda: meta_health.check_webhook_subscribed(
                    waba.waba_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id
                ))
            ]
            if connection.origin == "byok":
                checks.append(meta_health.check_own_app_subscribed(wabas))
            items["waba_subscribed"] = combine_checks("waba_subscribed", checks)

    # Webhook: la suscripción de la app de ESTA conexión al objeto de su canal (estado que
    # guarda el cron de 15 min) y, por número, que un override de webhook responda.
    webhook = [await safe_check("webhook", lambda: meta_health.check_app_subscription(connection))]
    if creds is not None and connection.channel == "whatsapp":
        for phone in phones:
            check = await safe_check("webhook", lambda phone=phone: meta_health.check_webhook_override(
                phone.phone_number_id, creds.access_token, creds.app_secret, creds.graph_api_version, connection.id
            ))
            webhook.append(_with_number(check, phone.display_phone_number if len(phones) > 1 else None))
    items["webhook"] = combine_checks("webhook", webhook)

    if "templates_approved" in keys:
        items["templates_approved"] = await safe_check(
            "templates_approved", lambda: check_minimum_templates(connection.id, tenant_id, [w.id for w in wabas])
        )

    return [items[key].model_copy(update={"key": key}) for key in keys]
