"""Credenciales de una Página de Messenger (ADR-0152), con BD real.

La Página manda con SU token (`page_access`), no con el de la cuenta ni con el de WhatsApp, y solo si la
Página es viva y de esta conexión. Cualquier otro caso devuelve `{}` y el envío falla: nunca se responde
por otra Página, por WhatsApp ni desde el `.env`.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, uniq
from sqlalchemy import update

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _new_tenant(prefix: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de credenciales de Messenger.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL, whatsapp=None,
    )
    return tid


async def _pagina_conectada(
    tid: uuid.UUID, page_id: str, *, token: str | None = "tok-de-la-pagina", status: str = "connected"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Cuenta de Meta de Messenger + su Página + su token + la conexión canónica. Devuelve
    `(meta_connection_id, channel_connection_id)`."""
    from core.db.models import ChannelConnection, FbPage, MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cuenta = MetaConnection(
                tenant_id=tid, channel="messenger", origin="manual", meta_app_id=uniq("app"), status=status,
            )
            session.add(cuenta)
            await session.flush()
            session.add(FbPage(tenant_id=tid, connection_id=cuenta.id, page_id=page_id))
            canonica = ChannelConnection(
                tenant_id=tid, channel="messenger", provider="meta", type_id="messenger_meta",
                category="communication", label=page_id, external_id=page_id, meta_connection_id=cuenta.id,
            )
            session.add(canonica)
            await session.flush()
            ids = (cuenta.id, canonica.id)
        if token:
            await meta_token_store.put_token(ids[0], "page_access", page_id, token, tenant_id=tid)
    return ids


async def _credenciales(tid: uuid.UUID, canonica: uuid.UUID, esperado: uuid.UUID | None = None):
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await resolve_credentials(canonica, None, esperado if esperado is not None else tid)


async def test_la_pagina_envia_con_su_propio_token() -> None:
    tid, page_id = await _new_tenant("msgr-ok"), uniq("page")
    _cuenta, canonica = await _pagina_conectada(tid, page_id)

    provider, credenciales = await _credenciales(tid, canonica)

    assert provider == "meta"
    assert credenciales["page_id"] == page_id
    assert credenciales["access_token"] == "tok-de-la-pagina"
    assert "phone_number_id" not in credenciales  # nunca las de WhatsApp


async def test_sin_token_de_la_pagina_no_hay_credenciales() -> None:
    tid = await _new_tenant("msgr-sintoken")
    _cuenta, canonica = await _pagina_conectada(tid, uniq("page"), token=None)

    assert await _credenciales(tid, canonica) == ("meta", {})


async def test_una_cuenta_pausada_no_envia() -> None:
    tid = await _new_tenant("msgr-pausada")
    _cuenta, canonica = await _pagina_conectada(tid, uniq("page"), status="revoked")

    assert await _credenciales(tid, canonica) == ("meta", {})


async def test_una_cuenta_eliminada_no_envia() -> None:
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant("msgr-borrada")
    cuenta, canonica = await _pagina_conectada(tid, uniq("page"))
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(update(MetaConnection).where(MetaConnection.id == cuenta).values(deleted_at=utcnow()))

    assert await _credenciales(tid, canonica) == ("meta", {})


async def test_el_tenant_de_la_conversacion_debe_ser_el_de_la_cuenta() -> None:
    tid, otro = await _new_tenant("msgr-dueno"), uuid.uuid4()
    _cuenta, canonica = await _pagina_conectada(tid, uniq("page"))

    assert await _credenciales(tid, canonica, esperado=otro) == ("meta", {})


async def test_una_pagina_que_no_es_de_la_cuenta_no_envia() -> None:
    """La conexión canónica apunta a una Página que la cuenta no tiene: ni su token ni otra Página."""
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant("msgr-ajena")
    _cuenta, canonica = await _pagina_conectada(tid, uniq("page"))
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(ChannelConnection).where(ChannelConnection.id == canonica).values(external_id=uniq("otra-page"))
            )

    assert await _credenciales(tid, canonica) == ("meta", {})


# ------------------------------------------------------------------ ventana de servicio
@pytest.mark.parametrize("channel", ["whatsapp", "messenger", "instagram"])
async def test_cada_mensaje_del_cliente_renueva_la_ventana_de_24h_en_los_canales_de_meta(channel: str) -> None:
    """Antes solo WhatsApp: la conversación de Messenger o Instagram quedaba sin ventana y el validador
    la trataba como abierta para siempre."""
    from datetime import UTC, datetime, timedelta

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.orchestrator.consumer import _persist_inbound
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant(f"ventana-{channel[:4]}")
    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel=channel, external_id=f"{channel}:{uniq('c')}")
            session.add(cliente)
            await session.flush()
            conversacion = Conversation(tenant_id=tid, customer_id=cliente.id, channel=channel)
            session.add(conversacion)
            await session.flush()
            conversacion_id = conversacion.id

        guardado = await _persist_inbound(
            conversacion_id,
            {"provider": "meta", "channel": channel, "external_message_id": uniq("mid"), "media": "[]"},
            "Hola",
        )
        assert guardado is not None
        async with get_session() as session:
            vence = (await session.get(Conversation, conversacion_id)).service_window_expires_at

    assert vence is not None
    assert timedelta(hours=23, minutes=55) < vence - datetime.now(UTC) <= timedelta(hours=24)
