"""Fixtures compartidos para los tests de integración del Sprint 1.

Requieren el stack real (Postgres + Redis) y se saltan salvo que se corra con
`AGENT_CORE_INTEGRATION=1`. El LLM y el canal están stubbeados: se prueba el
pipeline (dedupe, debounce, cuota por tenant, RLS), no el modelo.
"""

from __future__ import annotations

import os
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

    def __init__(self, recorded: list[str], delay_s: float = 0.0) -> None:
        self._recorded = recorded
        self._delay = delay_s

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


@pytest.fixture(scope="module")
def recorded() -> list[str]:
    return []


@pytest.fixture(autouse=True)
def _stub_llm_and_outbound(recorded: list[str], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: FakeProvider(recorded))
    monkeypatch.setattr("core.gateway.sender.outbound_adapter_for", lambda *a, **k: FakeOutbound())
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


@pytest.fixture(scope="module")
async def app_client() -> AsyncIterator[httpx.AsyncClient]:
    from core.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(scope="module")
async def tenant_setup():
    """Tenant #1 (seed) con conexión de canal + tenant #2. Devuelve (t1, t2)."""
    from sqlalchemy import select

    from core.db.models import ChannelConnection, Tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        t1 = (await session.execute(select(Tenant).where(Tenant.slug == "tenant-1"))).scalar_one()
        t2 = (await session.execute(select(Tenant).where(Tenant.slug == "tenant-2"))).scalar_one_or_none()
        if t2 is None:
            t2 = Tenant(name="Tenant #2", slug="tenant-2")
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
async def formoto_setup():
    """Asegura el tenant FORMOTO (onboarding idempotente). Devuelve (t1, formoto_id)."""
    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        formoto = (
            await session.execute(select(Tenant).where(Tenant.slug == "formoto"))
        ).scalar_one_or_none()
    if formoto is None:
        from scripts.onboard_formoto import onboard

        await onboard()
    async with get_session() as session:
        t1 = (await session.execute(select(Tenant).where(Tenant.slug == "tenant-1"))).scalar_one()
        formoto = (
            await session.execute(select(Tenant).where(Tenant.slug == "formoto"))
        ).scalar_one()
    return t1.id, formoto.id


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