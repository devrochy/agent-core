"""Checklist de promoción a producción -- los 9 checks automáticos del
ticket, cada uno devuelve `PreflightCheck` (reusado de `meta_preflight.py`,
mismo patrón que `meta_health.py`).

IMPORTANTE: la mayoría de estos checks solo dan una respuesta con sentido
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
- pgTap -> se reusan los tests pytest de aislamiento multi-tenant ya
  existentes, corridos por subprocess.
- `NEXT_PUBLIC_*` -> no hay bundler ni env vars expuestas al cliente en
  este panel Jinja2 server-rendered. Se reinterpreta como: ningún template
  interpola un campo cuyo nombre contenga SECRET/SERVICE_ROLE/TOKEN.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    verificar lo que Meta vería de verdad (DNS, TLS, proxy/middleware)."""
    now = datetime.now(UTC)
    challenge = uuid.uuid4().hex
    params = {"hub.mode": "subscribe", "hub.verify_token": verify_token, "hub.challenge": challenge}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(callback_url, params=params)
    except httpx.HTTPError as exc:
        return PreflightCheck(key="webhook_handshake", ok=False, detail=f"No se pudo contactar {callback_url}: {exc}", checked_at=now)

    content_type = response.headers.get("content-type", "")
    if response.status_code == 200 and response.text == challenge and content_type.startswith("text/plain"):
        return PreflightCheck(key="webhook_handshake", ok=True, detail="Handshake correcto", checked_at=now)
    return PreflightCheck(
        key="webhook_handshake", ok=False,
        detail=f"status={response.status_code} content-type={content_type!r} body={response.text[:100]!r}",
        checked_at=now,
    )


async def check_app_subscriptions_complete(meta_app_id: str) -> PreflightCheck:
    """Lee el estado ya persistido por el cron de 15 min
    (`meta_subscriptions_health.py`) -- no repite el GET, mismo criterio
    que `meta_health.py::check_app_subscription`."""
    from core.gateway.meta_subscriptions import OBJECT_FIELDS, list_app_subscriptions

    now = datetime.now(UTC)
    statuses = await list_app_subscriptions(meta_app_id)
    by_object = {s.object: s for s in statuses}

    missing = []
    for object_, min_fields in OBJECT_FIELDS.items():
        row = by_object.get(object_)
        if row is None or row.active is not True:
            missing.append(f"{object_} (active={row.active if row else 'sin suscripción'})")
            continue
        missing_fields = set(min_fields) - set(row.fields)
        if missing_fields:
            missing.append(f"{object_} (faltan fields: {sorted(missing_fields)})")

    if missing:
        return PreflightCheck(
            key="app_subscriptions_complete", ok=False, detail=f"Problemas: {'; '.join(missing)}", checked_at=now
        )
    return PreflightCheck(key="app_subscriptions_complete", ok=True, detail="Los 3 objetos activos con fields completos", checked_at=now)


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
    from core.orchestrator.consumer import DEAD_LETTER_STREAM

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

    dlq_size = await redis.xlen(DEAD_LETTER_STREAM)
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
    """Traduce "correr los tests pgTap de RLS" -- no existe pgTap acá, se
    reusan los tests pytest de aislamiento multi-tenant ya existentes."""
    now = datetime.now(UTC)
    import os

    env = {**os.environ, "AGENT_CORE_INTEGRATION": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/integration", "-k", "tenant or rls or aislad or aislamiento", "-q"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        return PreflightCheck(
            key="rls_tests_pass", ok=False, detail=f"pytest exit={result.returncode}: {result.stdout[-300:]}", checked_at=now
        )
    return PreflightCheck(key="rls_tests_pass", ok=True, detail="Tests de aislamiento multi-tenant en verde", checked_at=now)


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
