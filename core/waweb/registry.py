"""Registro de sesiones `waweb` que el connector debe mantener (ADR-0074).

Aísla el acceso a BD del `manager`: el manager habla con `SessionRegistry` (fake
en tests, `DbSessionRegistry` en producción).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select

from core.db.models import ChannelConnection, Tenant, WaWebSession, utcnow
from core.db.session import get_session
from core.shared.tenant import tenant_scope
from core.waweb import status


@dataclass(frozen=True)
class SessionContext:
    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    connection_external_id: str
    status: str
    # Dato DURABLE de que la sesión ya se vinculó alguna vez y conserva
    # credenciales (`last_connected_at` o `jid`, más `creds_encrypted`): a
    # diferencia de `status`, no cambia por un error o un reinicio. El manager
    # lo usa para no tratar como "nunca vinculada" una sesión que sí lo estuvo.
    linked_before: bool = False


class SessionRegistry(Protocol):
    async def active_sessions(self) -> list[SessionContext]: ...

    async def set_status(
        self,
        tenant_id: uuid.UUID,
        new_status: str,
        *,
        detail: str | None = None,
        jid: str | None = None,
    ) -> None: ...

    async def tenants_with_jid(self, jid: str) -> list[uuid.UUID]: ...


class DbSessionRegistry:
    """Implementación real contra `waweb_sessions`."""

    async def active_sessions(self) -> list[SessionContext]:
        """Sesiones que el connector debe correr: riesgo aceptado, no deslogueadas,
        con la conexión activa.

        El connector corre como `app_runtime` (RLS): para enumerar cross-tenant se
        itera `tenants` (tabla de sistema, sin RLS) y cada consulta corre dentro de
        `tenant_scope` — mismo patrón que `activate_all_configured` (ADR-0039).
        """
        async with get_session() as session:
            tenant_ids = list(
                (await session.execute(select(Tenant.id).where(Tenant.status == "active"))).scalars().all()
            )

        contexts: list[SessionContext] = []
        for tenant_id in tenant_ids:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    rows = (
                        await session.execute(
                            select(WaWebSession, ChannelConnection)
                            .join(ChannelConnection, ChannelConnection.id == WaWebSession.connection_id)
                            .where(
                                WaWebSession.tenant_id == tenant_id,
                                WaWebSession.deleted_at.is_(None),
                                WaWebSession.risk_ack_at.is_not(None),
                                WaWebSession.status != status.LOGGED_OUT,
                                ChannelConnection.tenant_id == tenant_id,
                                ChannelConnection.active.is_(True),
                                ChannelConnection.deleted_at.is_(None),
                            )
                        )
                    ).all()
            contexts.extend(
                SessionContext(
                    tenant_id=wa_session.tenant_id,
                    connection_id=wa_session.connection_id,
                    connection_external_id=connection.external_id,
                    status=wa_session.status,
                    linked_before=(
                        wa_session.last_connected_at is not None or bool(wa_session.jid)
                    )
                    and wa_session.creds_encrypted is not None,
                )
                for wa_session, connection in rows
                if wa_session.connection_id is not None
            )
        return contexts

    async def set_status(
        self,
        tenant_id: uuid.UUID,
        new_status: str,
        *,
        detail: str | None = None,
        jid: str | None = None,
    ) -> None:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                row = (
                    await session.execute(
                        select(WaWebSession).where(
                            WaWebSession.tenant_id == tenant_id,
                            WaWebSession.deleted_at.is_(None),
                        )
                    )
                ).scalars().first()
                if row is None:
                    return
                row.status = new_status
                if detail is not None:
                    row.last_error = detail
                if jid is not None:
                    row.jid = jid
                if new_status == status.CONNECTED:
                    row.last_connected_at = utcnow()
                    row.last_error = None
                else:
                    row.updated_at = utcnow()

    async def tenants_with_jid(self, jid: str) -> list[uuid.UUID]:
        """Tenants que tienen una sesión con ese `jid` (para detectar duplicados).

        Cross-tenant bajo RLS: itera `tenants` y scopea cada consulta, igual que
        `active_sessions`.
        """
        if not jid:
            return []
        async with get_session() as session:
            tenant_ids = list(
                (await session.execute(select(Tenant.id).where(Tenant.status == "active"))).scalars().all()
            )
        found: list[uuid.UUID] = []
        for tenant_id in tenant_ids:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    row = (
                        await session.execute(
                            select(WaWebSession.id).where(
                                WaWebSession.tenant_id == tenant_id,
                                WaWebSession.jid == jid,
                                WaWebSession.deleted_at.is_(None),
                            )
                        )
                    ).scalars().first()
            if row is not None:
                found.append(tenant_id)
        return found
