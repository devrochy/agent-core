"""Un evento del webhook de Meta solo actúa sobre el negocio que lo firmó (ADR-0096).

El endpoint genérico juntaba los secretos de todos los negocios del payload y aceptaba la
firma con cualquiera: un negocio con app propia (BYOK) firmaba con su secreto un POST con
una entry suya y otra con el número de A, y le inyectaba mensajes a A. Y con una firma
válida propia, los eventos de plantillas y de estado escribían en la plantilla o el
mensaje de otro negocio (los resuelven por su ID, también por el endpoint por slug).

BD, Redis y el app reales; la app de la plataforma se fija por monkeypatch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PLATAFORMA = "plataforma-secret-test"


@pytest.fixture(autouse=True)
def plataforma(monkeypatch):
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_app_secret", PLATAFORMA)
    monkeypatch.setattr(settings, "app_env", "production")
    return settings


# ------------------------------------------------------------------ helpers
async def _negocio(prefix: str, *, waba_id: str, phone: str, app_secret: str | None = None) -> uuid.UUID:
    """Negocio con una conexión de Meta viva, su WABA y su número. Con `app_secret`, es
    BYOK (secreto propio); sin él, usa la app de la plataforma."""
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_byok import APP_SECRET_KIND
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de firma por negocio.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL,
    )
    app_id = uniq("app")
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", origin="byok" if app_secret else "embedded_signup",
                meta_app_id=app_id, status="connected",
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id)
            session.add(waba)
            await session.flush()
            session.add(
                PhoneNumber(
                    tenant_id=tid, connection_id=connection.id, waba_account_id=waba.id, phone_number_id=phone
                )
            )
            connection_id = connection.id
        if app_secret:
            await meta_token_store.put_token(connection_id, APP_SECRET_KIND, app_id, app_secret, tenant_id=tid)
    return tid


async def _slug_de(tid: uuid.UUID) -> str:
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        return (await session.get(Tenant, tid)).slug


def _mensaje(phone: str, wa_id: str, msg_id: str) -> dict:
    return {
        "field": "messages",
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone},
            "contacts": [{"wa_id": wa_id, "profile": {"name": "Cliente"}}],
            "messages": [{"from": wa_id, "id": msg_id, "type": "text", "text": {"body": "hola"}}],
        },
    }


async def _post(app_client, entries: list[dict], secreto: str, *, slug: str | None = None):
    body = json.dumps({"object": "whatsapp_business_account", "entry": entries}).encode()
    firma = "sha256=" + hmac.new(secreto.encode(), body, hashlib.sha256).hexdigest()
    url = "/webhooks/whatsapp" + (f"/{slug}" if slug else "")
    return await app_client.post(
        url, content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": firma}
    )


async def _evento(external_id: str):
    from core.db.models import WebhookEvent
    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(select(WebhookEvent).where(WebhookEvent.external_id == external_id))
        ).scalar_one_or_none()


async def _encolado(msg_id: str) -> bool:
    from core.gateway.webhooks import INBOUND_STREAM
    from core.memory.working import get_redis

    entradas = await get_redis().xrevrange(INBOUND_STREAM, count=500)
    return any(campos.get("external_message_id") == msg_id for _id, campos in entradas)


# ------------------------------------------------------------------ mensajes
async def test_un_negocio_no_puede_firmar_mensajes_de_otro(app_client) -> None:
    """El ataque: B (BYOK) firma con su secreto un POST con su entry y una con el número de A."""
    waba_a, phone_a, waba_b, phone_b = uniq("waba-a"), uniq("phone-a"), uniq("waba-b"), uniq("phone-b")
    tenant_a = await _negocio("firma-a", waba_id=waba_a, phone=phone_a)
    tenant_b = await _negocio("firma-b", waba_id=waba_b, phone=phone_b, app_secret="secreto-de-b")

    msg_a, msg_b = f"wamid.{uniq('a')}", f"wamid.{uniq('b')}"
    r = await _post(
        app_client,
        [
            {"id": waba_b, "changes": [_mensaje(phone_b, "wa-cliente-b", msg_b)]},
            {"id": waba_a, "changes": [_mensaje(phone_a, "wa-victima", msg_a)]},
        ],
        "secreto-de-b",
    )

    assert r.status_code == 200, r.text
    assert r.json()["enqueued"] == 1
    fila_a, fila_b = await _evento(msg_a), await _evento(msg_b)
    assert (fila_a.tenant_id, fila_a.process_error, fila_a.signature_ok) == (None, "signature_tenant_mismatch", False)
    assert (fila_b.tenant_id, fila_b.process_error) == (tenant_b, None)
    assert not await _encolado(msg_a)
    assert await _encolado(msg_b)
    assert tenant_a != tenant_b


async def test_solo_la_entry_de_otro_negocio_da_401(app_client) -> None:
    waba_a, phone_a = uniq("waba-a"), uniq("phone-a")
    await _negocio("solo-a", waba_id=waba_a, phone=phone_a)
    await _negocio("solo-b", waba_id=uniq("waba-b"), phone=uniq("phone-b"), app_secret="secreto-solo-b")

    msg_a = f"wamid.{uniq('a')}"
    r = await _post(app_client, [{"id": waba_a, "changes": [_mensaje(phone_a, "wa-x", msg_a)]}], "secreto-solo-b")

    assert r.status_code == 401
    assert await _evento(msg_a) is None


async def test_la_firma_de_la_plataforma_vale_para_todos_los_negocios(app_client) -> None:
    """Incluido uno BYOK: es lo que pasa con una conexión BYOK tomada por Embedded Signup."""
    waba_a, phone_a, waba_b, phone_b = uniq("waba-a"), uniq("phone-a"), uniq("waba-b"), uniq("phone-b")
    tenant_a = await _negocio("plat-a", waba_id=waba_a, phone=phone_a)
    tenant_b = await _negocio("plat-b", waba_id=waba_b, phone=phone_b, app_secret="secreto-plat-b")

    msg_a, msg_b = f"wamid.{uniq('a')}", f"wamid.{uniq('b')}"
    r = await _post(
        app_client,
        [
            {"id": waba_a, "changes": [_mensaje(phone_a, "wa-a", msg_a)]},
            {"id": waba_b, "changes": [_mensaje(phone_b, "wa-b", msg_b)]},
        ],
        PLATAFORMA,
    )

    assert r.status_code == 200, r.text
    assert r.json()["enqueued"] == 2
    assert (await _evento(msg_a)).tenant_id == tenant_a
    assert (await _evento(msg_b)).tenant_id == tenant_b


async def test_en_desarrollo_un_grupo_sin_resolver_firmado_por_un_negocio_no_cae_al_default(
    app_client, monkeypatch, plataforma
) -> None:
    import core.shared.tenant as tenant_mod

    waba_b, phone_b = uniq("waba-b"), uniq("phone-b")
    tenant_a = await _negocio("dev-a", waba_id=uniq("waba-a"), phone=uniq("phone-a"))
    await _negocio("dev-b", waba_id=waba_b, phone=phone_b, app_secret="secreto-dev-b")
    monkeypatch.setattr(plataforma, "app_env", "development")

    async def default_es_a():
        return tenant_a

    monkeypatch.setattr(tenant_mod, "default_tenant", default_es_a)

    msg_b, msg_suelto = f"wamid.{uniq('b')}", f"wamid.{uniq('x')}"
    r = await _post(
        app_client,
        [
            {"id": waba_b, "changes": [_mensaje(phone_b, "wa-b", msg_b)]},
            {"id": uniq("waba-sin-dueno"), "changes": [_mensaje(uniq("phone-sin-dueno"), "wa-x", msg_suelto)]},
        ],
        "secreto-dev-b",
    )

    assert r.status_code == 200, r.text
    fila = await _evento(msg_suelto)
    assert (fila.tenant_id, fila.process_error) == (None, "signature_tenant_mismatch")


# ------------------------------------------------------------------ plantillas y estados
async def _plantilla(tid: uuid.UUID, waba_id: str) -> tuple[uuid.UUID, str]:
    from core.db.models import MessageTemplate, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    meta_template_id = uniq("tpl")
    async with tenant_scope(tid):
        async with get_session() as session:
            waba = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))).scalar_one()
            row = MessageTemplate(
                tenant_id=tid, waba_account_id=waba.id, meta_template_id=meta_template_id,
                name="bienvenida", language="es", category="UTILITY", status="APPROVED",
            )
            session.add(row)
            await session.flush()
            return row.id, meta_template_id


async def _estado_plantilla(tid: uuid.UUID, row_id: uuid.UUID) -> str:
    from core.db.models import MessageTemplate
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (await session.get(MessageTemplate, row_id)).status


def _cambio_plantilla(meta_template_id: str, event: str) -> dict:
    return {
        "field": "message_template_status_update",
        "value": {"event": event, "message_template_id": meta_template_id, "message_template_name": "bienvenida"},
    }


@pytest.mark.parametrize("por_slug", [False, True], ids=["generico", "slug"])
async def test_un_negocio_no_puede_cambiar_una_plantilla_de_otro(app_client, por_slug) -> None:
    waba_a, waba_b = uniq("waba-a"), uniq("waba-b")
    tenant_a = await _negocio("tpl-a", waba_id=waba_a, phone=uniq("phone-a"))
    tenant_b = await _negocio("tpl-b", waba_id=waba_b, phone=uniq("phone-b"), app_secret="secreto-tpl-b")
    fila_a, tpl_a = await _plantilla(tenant_a, waba_a)
    fila_b, tpl_b = await _plantilla(tenant_b, waba_b)

    # Una sola entry, con la WABA de B: la plantilla de B (control) y la de A.
    r = await _post(
        app_client,
        [{"id": waba_b, "changes": [_cambio_plantilla(tpl_b, "PAUSED"), _cambio_plantilla(tpl_a, "DISABLED")]}],
        "secreto-tpl-b",
        slug=await _slug_de(tenant_b) if por_slug else None,
    )

    assert r.status_code == 200, r.text
    assert await _estado_plantilla(tenant_b, fila_b) == "PAUSED"
    assert await _estado_plantilla(tenant_a, fila_a) == "APPROVED"


async def _mensaje_enviado(tid: uuid.UUID) -> tuple[uuid.UUID, str]:
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    wamid = f"wamid.{uniq('enviado')}"
    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id=uniq("wa"))
            session.add(customer)
            await session.flush()
            conversation = Conversation(tenant_id=tid, customer_id=customer.id)
            session.add(conversation)
            await session.flush()
            message = Message(
                tenant_id=tid, conversation_id=conversation.id, direction="outbound", sender_type="agent",
                content="hola", status="sent", external_message_id=wamid,
            )
            session.add(message)
            await session.flush()
            return message.id, wamid


async def _estado_y_costos(tid: uuid.UUID, message_id: uuid.UUID, wamid: str) -> tuple[str, int]:
    from core.db.models import Message, MessageCost
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            status = (await session.get(Message, message_id)).status
            costos = (
                await session.execute(select(MessageCost).where(MessageCost.external_message_id == wamid))
            ).scalars().all()
    return status, len(costos)


@pytest.mark.parametrize("por_slug", [False, True], ids=["generico", "slug"])
async def test_un_negocio_no_puede_cambiar_el_estado_de_un_mensaje_de_otro(app_client, por_slug) -> None:
    waba_b, phone_b = uniq("waba-b"), uniq("phone-b")
    tenant_a = await _negocio("est-a", waba_id=uniq("waba-a"), phone=uniq("phone-a"))
    tenant_b = await _negocio("est-b", waba_id=waba_b, phone=phone_b, app_secret="secreto-est-b")
    msg_a, wamid_a = await _mensaje_enviado(tenant_a)
    msg_b, wamid_b = await _mensaje_enviado(tenant_b)

    pricing = {"billable": True, "pricing_model": "CBP", "category": "marketing"}
    estados = {
        "field": "messages",
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone_b},
            "statuses": [
                {"id": wamid_b, "status": "delivered", "timestamp": "1900000000", "pricing": pricing},
                {"id": wamid_a, "status": "failed", "timestamp": "1900000000", "pricing": pricing,
                 "errors": [{"code": 131047, "message": "inyectado"}]},
            ],
        },
    }
    r = await _post(
        app_client, [{"id": waba_b, "changes": [estados]}], "secreto-est-b",
        slug=await _slug_de(tenant_b) if por_slug else None,
    )

    assert r.status_code == 200, r.text
    assert await _estado_y_costos(tenant_b, msg_b, wamid_b) == ("delivered", 1)
    assert await _estado_y_costos(tenant_a, msg_a, wamid_a) == ("sent", 0)
    assert (await _estado_y_costos(tenant_b, msg_b, wamid_a))[1] == 0


# ------------------------------------------------------------------ cron de "worker caído"
async def test_los_eventos_rechazados_no_cuentan_como_worker_caido() -> None:
    """Los rechazados nunca se marcan procesados: sin el filtro, el cron alertaría cada
    hora para siempre (y un negocio podría provocarlo a voluntad)."""
    from datetime import timedelta

    from sqlalchemy import update

    from core.db.models import WebhookEvent, utcnow
    from core.db.session import get_session
    from core.gateway.webhook_flatten import FlattenedEvent
    from core.gateway.webhooks import _log_webhook_event, count_stalled_webhook_events

    antes = await count_stalled_webhook_events()
    evento = FlattenedEvent(
        object_type="whatsapp_business_account", field="messages", external_id=f"wamid.{uniq('viejo')}",
        entry_id="waba", sub_id="phone", value={},
    )
    assert await _log_webhook_event(
        "app-test", evento, tenant_id=None, process_error="signature_tenant_mismatch", signature_ok=False
    )
    async with get_session() as session:
        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.external_id == evento.external_id)
            .values(received_at=utcnow() - timedelta(hours=1))
        )

    assert await count_stalled_webhook_events() == antes
