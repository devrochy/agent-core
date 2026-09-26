"""Catálogo de tipos de conexión contra la BD real (ADR-0069).

Cubre: sync idempotente del catálogo, que una conexión que no es de mensajería
no robe la primacía, y que dos tenants con el mismo `external_id` no se pisen.
No depende de un tenant sembrado: usa tenants nuevos (UUID) y limpia al final.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_EMAIL = {
    "host": "smtp.ejemplo.com",
    "port": 587,
    "username": "u",
    "password": "p",
    "from_address": "",
    "use_tls": True,
}

_WHATSAPP = {
    "phone_number_id": "",
    "app_secret": "s",
    "access_token": "t",
    "verify_token": "v",
}


async def _cleanup(tenant_id, connection_ids) -> None:
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            for cid in connection_ids:
                row = await session.get(ChannelConnection, cid)
                if row is not None:
                    await session.delete(row)


async def test_sync_connection_types_idempotente():
    from core.db.models import ConnectionTypeRecord
    from core.db.session import get_session
    from core.gateway.connection_types import CONNECTION_TYPES, sync_connection_types

    first = await sync_connection_types()
    second = await sync_connection_types()
    assert first == second == len(CONNECTION_TYPES)

    async with get_session() as session:
        count = (
            await session.execute(select(func.count()).select_from(ConnectionTypeRecord))
        ).scalar()
    assert count >= len(CONNECTION_TYPES)


async def test_primacia_no_la_roba_una_conexion_no_mensajeria():
    from core.gateway.connections import (
        primary_connection,
        resolve_connection_credentials,
        resolve_primary_credentials,
        save_connection,
    )
    from core.shared.tenant import tenant_scope

    tenant_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    created: list[uuid.UUID] = []
    try:
        async with tenant_scope(tenant_id):
            email_creds = dict(_EMAIL, from_address=f"{suffix}@ejemplo.com")
            email, _ = await save_connection(
                "correo", email_creds, type_id="email_smtp"
            )
            created.append(email.id)
            assert email.is_primary is False, "una conexión de correo no es primaria"

            wa_creds = dict(_WHATSAPP, phone_number_id=f"pn-{suffix}")
            wa, _ = await save_connection("wa", wa_creds, type_id="whatsapp_meta")
            created.append(wa.id)
            assert wa.is_primary is True, "la primera de mensajería sí es primaria"

            primary = await primary_connection(tenant_id)
            assert primary is not None and primary.id == wa.id

            creds = await resolve_primary_credentials(tenant_id)
            assert creds is not None
            assert creds["phone_number_id"] == f"pn-{suffix}"

            by_type = await resolve_connection_credentials("email_smtp", tenant_id)
            assert by_type is not None
            assert by_type["from_address"] == f"{suffix}@ejemplo.com"
    finally:
        await _cleanup(tenant_id, created)


async def test_dos_tenants_mismo_external_id_no_se_pisan():
    from core.gateway.connections import resolve_connection_credentials, save_connection
    from core.shared.tenant import tenant_scope

    shared = f"compartido-{uuid.uuid4().hex[:8]}@ejemplo.com"
    tenants = [uuid.uuid4(), uuid.uuid4()]
    created: dict[uuid.UUID, list[uuid.UUID]] = {t: [] for t in tenants}
    try:
        ids = []
        for tenant_id in tenants:
            async with tenant_scope(tenant_id):
                row, was_created = await save_connection(
                    "correo",
                    dict(_EMAIL, from_address=shared),
                    type_id="email_smtp",
                )
                created[tenant_id].append(row.id)
                ids.append(row.id)
                assert was_created is True, "cada tenant crea su propia conexión"

        assert ids[0] != ids[1], "mismo external_id en dos tenants no puede ser una sola fila"

        for tenant_id in tenants:
            async with tenant_scope(tenant_id):
                creds = await resolve_connection_credentials("email_smtp", tenant_id)
                assert creds is not None and creds["from_address"] == shared
    finally:
        for tenant_id, conn_ids in created.items():
            await _cleanup(tenant_id, conn_ids)


async def test_sender_fallback_ignora_una_primaria_no_mensajeria():
    """Regresión: dos primarias (mensajería + correo) no rompen el sender.

    Antes de ADR-0069 el fallback de `sender.resolve_credentials` tomaba cualquier
    primaria activa con `scalar_one_or_none()` → `MultipleResultsFound` (crash de
    envío) en cuanto existía una primaria de correo además de la de WhatsApp.
    """
    from core.gateway.connections import save_connection, set_primary
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tenant_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    created: list[uuid.UUID] = []
    try:
        async with tenant_scope(tenant_id):
            wa, _ = await save_connection(
                "wa",
                dict(_WHATSAPP, phone_number_id=f"pn-{suffix}"),
                type_id="whatsapp_meta",
            )
            created.append(wa.id)
            email, _ = await save_connection(
                "correo",
                dict(_EMAIL, from_address=f"{suffix}@ejemplo.com"),
                type_id="email_smtp",
            )
            created.append(email.id)
            # Marca la de correo como primaria (de su categoría): coexisten dos.
            await set_primary(email.id)

            provider, creds = await resolve_credentials(None, None)
            assert provider == "meta"
            assert creds["phone_number_id"] == f"pn-{suffix}"
    finally:
        await _cleanup(tenant_id, created)