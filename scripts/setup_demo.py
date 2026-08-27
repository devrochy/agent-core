"""Prepara el entorno demo (dev): password admin, conexión WhatsApp y CRM activo."""

import asyncio
import uuid

from sqlalchemy import select

from core.admin.auth import hash_password
from core.db.models import Admin, ChannelConnection
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from core.shared.tenant import tenant_scope

ADMIN_EMAIL = "admin@local"
ADMIN_PASSWORD = "admin123"
T1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
PHONE = "1234567890"


async def main() -> None:
    plugin_manager.scan()
    # 1. password del admin conocido (demo)
    async with get_session() as session:
        admin = (await session.execute(select(Admin).where(Admin.email == ADMIN_EMAIL))).scalar_one_or_none()
        if admin is not None:
            admin.password_hash = hash_password(ADMIN_PASSWORD)
            print(f"✓ Password de {ADMIN_EMAIL} -> {ADMIN_PASSWORD}")

    # 2. conexión WhatsApp (fake) para resolver el tenant del webhook
    async with tenant_scope(T1):
        async with get_session() as session:
            conn = (
                await session.execute(
                    select(ChannelConnection).where(ChannelConnection.external_id == PHONE)
                )
            ).scalar_one_or_none()
            if conn is None:
                session.add(
                    ChannelConnection(
                        tenant_id=T1,
                        channel="whatsapp",
                        provider="meta",
                        label="Demo WhatsApp",
                        external_id=PHONE,
                        active=True,
                        is_primary=True,
                    )
                )
                print("✓ Conexión WhatsApp (fake) creada para", PHONE)
            else:
                print("✓ Conexión WhatsApp ya existía")

    # 3. CRM configurado y activado (defaults)
    async with tenant_scope(T1):
        await plugin_manager.save_config("mod_crm_engine", {})
        await plugin_manager.activate("mod_crm_engine")
    async with tenant_scope(T1):
        from core.mcp.host import mcp_host

        print("✓ CRM activo · tools registradas:", len(mcp_host._tools))


asyncio.run(main())