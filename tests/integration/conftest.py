"""Fixtures compartidos para los tests de integración del Sprint 1.

Requieren el stack real (Postgres + Redis) y se saltan salvo que se corra con
`AGENT_CORE_INTEGRATION=1`. El LLM y el canal están stubbeados: se prueba el
pipeline (dedupe, debounce, cuota por tenant, RLS), no el modelo.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

# El conftest raíz (tests/) fija una DATABASE_URL falsa para los tests unitarios
# (setdefault). Los tests de integración necesitan el stack REAL: se carga el .env
# con override y se limpia la caché de settings ANTES de importar nada del core.
ROOT = Path(__file__).resolve().parents[2]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from core.config import get_settings  # noqa: E402

get_settings.cache_clear()

import types  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_CORE_INTEGRATION") != "1",
    reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
)

T1 = "00000000-0000-0000-0000-000000000001"
PHONE = "1234567890"

# UUIDs dedicados para los tenants de PRUEBA (espacio reservado, no colisiona con
# tenants reales; 0000...0002 está ocupado por formoto).
TEST_ALPHA_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
TEST_BETA_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = text
        self.stop_reason = "end_turn"
        self.tool_calls: list[Any] = []
        self.model = "fake-model"
        self.latency_ms = 5
        self.usage = types.SimpleNamespace(input_tokens=10, output_tokens=5)


class FakeProvider:
    provider_name = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, recorded: list[str], delay_s: float = 0.0) -> None:
        self._recorded = recorded
        self._delay = delay_s

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        cin, cout = self.pricing_per_mtok
        return (input_tokens * cin + output_tokens * cout) / 1_000_000

    async def generate(self, **kwargs: Any) -> FakeResponse:
        import asyncio

        if self._delay:
            await asyncio.sleep(self._delay)
        messages = kwargs.get("messages") or []
        last_user = ""
        for m in reversed(messages):
            if getattr(m, "role", None) == "user":
                last_user = str(getattr(m, "content", ""))
                break
        self._recorded.append(last_user)
        return FakeResponse("respuesta del bot")


class FakeOutbound:
    async def send_text(self, credentials: dict[str, Any], to: str, text: str) -> None:
        return None

    async def send_image(self, credentials: dict[str, Any], to: str, url: str, text: str) -> None:
        return None


@pytest.fixture(scope="session", autouse=True)
def _alta_sin_agent_os():
    """Casi todos los tests de integración modelan negocios «de antes»: legacy y sin agentes (lo que dejaba el
    alta hasta HU-AOS-53). El alta de hoy crea el General y el Coordinador y, con el ruteo encendido, deja el
    negocio en v3; esos dos pasos se apagan aquí y solo los reactiva `test_alta_agentes_base` (con
    `_alta_agent_os.ACTIVO = True`)."""
    import _alta_agent_os

    import core.alta_negocio as alta

    reales = (alta._agentes_base_al_alta, alta._runtime_al_alta)

    async def _agentes(tenant_id: uuid.UUID, *, nuevo: bool) -> list[str]:
        return await reales[0](tenant_id, nuevo=nuevo) if _alta_agent_os.ACTIVO else []

    async def _runtime(tenant_id: uuid.UUID) -> str:
        return await reales[1](tenant_id) if _alta_agent_os.ACTIVO else "runtime v3 no aplicado (test)"

    alta._agentes_base_al_alta = _agentes  # type: ignore[assignment]
    alta._runtime_al_alta = _runtime  # type: ignore[assignment]
    yield
    alta._agentes_base_al_alta, alta._runtime_al_alta = reales  # type: ignore[assignment]


@pytest.fixture(scope="module")
def recorded() -> list[str]:
    return []


@pytest.fixture(autouse=True)
def _stub_llm_and_outbound(recorded: list[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: FakeProvider(recorded))
    # El envío real ahora pasa por la tarea Celery (`core/tasks/outbound_send.py`),
    # no por `sender.py` directamente -- se parchea ahí.
    monkeypatch.setattr("core.tasks.outbound_send.outbound_adapter_for", lambda *a, **k: FakeOutbound())
    return recorded


@pytest.fixture(autouse=True)
async def _clean_redis():
    """Limpia streams/colas/contadores de Redis antes de cada test (los smokes y
    ejecuciones previas dejan entradas que contaminan los conteos)."""
    from core.memory.working import get_redis

    r = get_redis()
    for pattern in ("agent_core:*", "debounce:*", "inbound:*", "turns:*"):
        keys = await r.keys(pattern)
        for key in keys:
            await r.delete(key)
    yield


@pytest.fixture(autouse=True)
async def _reset_test_tenants_state():
    """Prepara los tenants de PRUEBA para un arranque limpio.

    Contrarresta la contaminación de una BD local reusada entre corridas:
    1. Un test anterior deja `conversations.status='escalated'`/`bot_paused` para
       un `customer_external_id` fijo (wa-555, wa-777, ...), y el consumidor
       descarta el inbound nuevo de ese contacto sin procesarlo.
    2. El tenant de prueba acumula miles de `customers`/`conversations` de
       corridas previas, superando la cuota del plan `free` (ADR-0045) y haciendo
       que el gate de billing bloquee turnos legítimos del test.

    Solo toca los UUIDs reservados de test, nunca tenants reales.
    """
    from sqlalchemy import select

    from core.db.models import ChannelConnection, Conversation, Customer, TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    test_ids = (TEST_ALPHA_ID, TEST_BETA_ID)

    async def _reset() -> None:
        for tid in test_ids:
            async with tenant_scope(tid):
                async with get_session() as session:
                    # Conexiones primarias duplicadas: `resolve_credentials`
                    # (`sender.py`) usa `scalar_one_or_none` y revienta con
                    # `MultipleResultsFound` si quedan varias de corridas previas.
                    # Se deja UNA primaria (la de `tenant_setup`, external_id=PHONE).
                    primaries = (
                        await session.execute(
                            select(ChannelConnection).where(
                                ChannelConnection.tenant_id == tid,
                                ChannelConnection.is_primary.is_(True),
                                ChannelConnection.active.is_(True),
                                ChannelConnection.deleted_at.is_(None),
                            )
                        )
                    ).scalars().all()
                    if len(primaries) > 1:
                        keeper = next(
                            (c for c in primaries if c.external_id == PHONE), primaries[0]
                        )
                        for conn in primaries:
                            if conn.id != keeper.id:
                                conn.is_primary = False
                    for conv in (
                        await session.execute(
                            select(Conversation).where(
                                Conversation.tenant_id == tid,
                                Conversation.status.in_(["open", "escalated"]),
                            )
                        )
                    ).scalars().all():
                        conv.status = "closed"
                        conv.bot_paused = False
                    for cust in (
                        await session.execute(
                            select(Customer).where(Customer.tenant_id == tid, Customer.bot_paused.is_(True))
                        )
                    ).scalars().all():
                        cust.bot_paused = False
                    # Plan de prueba con período largo: el enforcement de cuotas
                    # se prueba en `test_billing_*`, no debe gatear los tests de
                    # pipeline que reusan el tenant acumulado.
                    sub = (
                        await session.execute(
                            select(TenantSubscription).where(
                                TenantSubscription.tenant_id == tid,
                                TenantSubscription.deleted_at.is_(None),
                            )
                        )
                    ).scalar_one_or_none()
                    if sub is not None:
                        # Plan empresarial (ilimitado): los tests de pipeline no
                        # deben gatearse por cuotas acumuladas de corridas previas.
                        sub.plan_id = "empresarial"
                        sub.status = "active"
                        from datetime import UTC, datetime, timedelta

                        sub.period_end = datetime.now(UTC) + timedelta(days=3650)
                        sub.trial_ends_at = None

    await _reset()
    yield
    await _reset()


@pytest.fixture(scope="module")
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    from core.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(scope="module")
async def tenant_setup():
    """Dos tenants de PRUEBA auto-contenidos (no dependen del seed del framework).

    Usa UUIDs fijos conocidos (T1/T2) para que las referencias de los tests sigan
    valiendo sin exigir que `tenant-1` exista en la BD (la suite crea los suyos).
    Devuelve (t1, t2) con conexiones de canal.
    """
    from sqlalchemy import select

    from core.db.models import ChannelConnection, Tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        t1 = await session.get(Tenant, TEST_ALPHA_ID)
        if t1 is None:
            t1 = Tenant(id=TEST_ALPHA_ID, name="Test Alpha", slug="test-alpha", status="active")
            session.add(t1)
            await session.flush()
        t2 = await session.get(Tenant, TEST_BETA_ID)
        if t2 is None:
            t2 = Tenant(id=TEST_BETA_ID, name="Test Beta", slug="test-beta", status="active")
            session.add(t2)
            await session.flush()
        t1_id, t2_id = t1.id, t2.id

    for tid in (t1_id, t2_id):
        async with tenant_scope(tid):
            async with get_session() as session:
                conn = (
                    await session.execute(
                        select(ChannelConnection).where(
                            ChannelConnection.external_id == PHONE if tid == t1_id else ChannelConnection.external_id == "999"
                        )
                    )
                ).scalar_one_or_none()
                if conn is None:
                    session.add(
                        ChannelConnection(
                            tenant_id=tid,
                            channel="whatsapp",
                            provider="meta",
                            label="it",
                            external_id=PHONE if tid == t1_id else "999",
                        )
                    )
    return t1_id, t2_id


@pytest.fixture(scope="module")
async def moto_setup():
    """Tenant de ejemplo genérico con pack_commerce + catálogo (ADR-0001).

    Onboarding idempotente vía core.onboarding + seed del catálogo desde
    tests/fixtures/tenants/moto/catalog.json. Devuelve (t1, moto_id).
    """
    import json

    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    MOTO_SLUG = "tenant-moto"
    CATALOG_PATH = (
        Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "tenants" / "moto" / "catalog.json"
    )

    # Siempre a través de onboard_tenant (idempotente: si el tenant ya existe,
    # solo rellena lo que falte -- ej. una BD local vieja con el Tenant creado
    # pero sin AppSettings, de antes de que `escalation_config` existiera).
    # `seed_catalog` también es idempotente (docstring propio).
    from cli.commands.tenant import seed_catalog
    from core.alta_negocio import onboard_tenant

    moto_id, _ = await onboard_tenant(
        name="Tenant de ejemplo (moto)",
        slug=MOTO_SLUG,
        business_description="Tienda de repuestos y accesorios de moto (ejemplo genérico).",
        persona={"role": "asistente de ventas", "tone": "cercano"},
        escalation_triggers=["asesor", "humano", "reclamo", "queja", "compatibilidad"],
        packs=["pack_commerce"],
        admin_email="admin@local",
    )
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    await seed_catalog(moto_id, catalog)

    # El admin de tests siempre debe poder ver el tenant de ejemplo (idempotente).
    from core.db.models import Admin, TenantUser

    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == "admin@local"))
        ).scalar_one_or_none()
        if admin is not None:
            link = (
                await session.execute(
                    select(TenantUser).where(
                        TenantUser.tenant_id == moto_id, TenantUser.admin_id == admin.id
                    )
                )
            ).scalar_one_or_none()
            if link is None:
                session.add(TenantUser(tenant_id=moto_id, admin_id=admin.id, role="owner"))
                await session.commit()

    async with get_session() as session:
        # t1 = el tenant de prueba "alpha" (TEST_ALPHA_ID); lo crea tenant_setup si falta.
        t1 = await session.get(Tenant, TEST_ALPHA_ID)
        if t1 is None:
            t1 = Tenant(id=TEST_ALPHA_ID, name="Test Alpha", slug="test-alpha", status="active")
            session.add(t1)
            await session.flush()
        moto = (
            await session.execute(select(Tenant).where(Tenant.slug == MOTO_SLUG))
        ).scalar_one()
    return t1.id, moto.id


def _meta_payload(phone_id: str, wa_id: str, message_id: str, body: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "e1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_id},
                            "contacts": [{"wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": message_id,
                                    "timestamp": "1700000000",
                                    "text": {"body": body},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _sign_meta(body: bytes, secret: str) -> dict[str, str]:
    """Cabecera `X-Hub-Signature-256` con el HMAC del App Secret efectivo.

    El webhook GENÉRICO (`/webhooks/whatsapp`) verifica la firma cuando hay un
    App Secret configurado (`.env` `WHATSAPP_APP_SECRET` o BYOK del tenant). Los
    tests que postean sin firmar dan 401 en cuanto el entorno tiene el secreto
    (BYOK E2E en `.env`) -- antes pasaban solo porque el `.env` local no lo
    tenía. Helper compartido para no repetir el HMAC en cada test.
    """
    import hashlib
    import hmac

    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def _meta_request(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Cuerpo crudo + headers firmados con el App Secret de `.env` (webhook genérico)."""
    import json as _json

    from core.config import get_settings

    body = _json.dumps(payload).encode()
    secret = get_settings().whatsapp_app_secret or get_settings().meta_app_secret or ""
    headers = {"Content-Type": "application/json"}
    if secret:
        headers.update(_sign_meta(body, secret))
    return body, headers