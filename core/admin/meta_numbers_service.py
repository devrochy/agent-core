"""Números telefónicos de una conexión Meta (fuente única SSR + API).

Envuelve el gateway de Meta (`sync_phone_numbers`, `add_phone_number`,
`request_verification_code`, `verify_code`) con la resolución de WABA/token y el
rate limit de reenvío de código. Cada función devuelve un error legible o `None`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.db.models import MetaConnection, PhoneNumber, WabaAccount
from core.db.session import get_owned, get_session


async def resolve_waba_and_token(connection_id: uuid.UUID, tenant_id: uuid.UUID | None):
    """(connection, waba, token) del tenant; cualquiera puede ser None."""
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND

    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, connection_id, tenant_id)
        if connection is None:
            return None, None, None
        waba = (
            await session.execute(
                select(WabaAccount).where(WabaAccount.connection_id == connection_id)
            )
        ).scalars().first()
    if waba is None:
        return connection, None, None
    token = await meta_token_store.get_token(
        connection_id, TOKEN_KIND, connection.business_id or str(connection_id), tenant_id=tenant_id
    )
    return connection, waba, token


async def list_numbers(
    tenant_id: uuid.UUID, connection_id: str
) -> tuple[list[dict], str | None]:
    """Sincroniza con Meta y devuelve los números del tenant. (números, error)."""
    from core.config import get_settings
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import sync_phone_numbers

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return [], "Conexión inválida"

    connection, waba, token = await resolve_waba_and_token(cid, tenant_id)
    if connection is None:
        return [], "Conexión no encontrada"

    sync_error = None
    if waba is not None and token:
        try:
            await sync_phone_numbers(
                waba.waba_id,
                token,
                get_settings().meta_app_secret,
                connection.graph_api_version,
                connection.id,
                tenant_id,
            )
        except GraphError as exc:
            sync_error = exc.message

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.connection_id == connection.id,
                        PhoneNumber.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    numbers = [
        {
            "id": n.id,
            "phone_number_id": n.phone_number_id,
            "display_phone_number": n.display_phone_number,
            "verified_name": n.verified_name,
            # `PhoneNumber` no tiene columna `status` (N-18): se deriva de los
            # campos reales que sí existen, para el API JSON del panel.
            "status": _number_status(n),
        }
        for n in rows
    ]
    return numbers, sync_error


def _number_status(n) -> str:  # type: ignore[no-untyped-def]
    """Estado visible del número a partir de las columnas reales.

    Prioridad: verificado > conectado > estado del nombre > 'PENDING'.
    """
    if (n.code_verification_status or "").upper() == "VERIFIED":
        return "VERIFIED"
    if n.registered:
        return "CONNECTED"
    return n.name_status or "PENDING"


async def add_number(
    tenant_id: uuid.UUID,
    connection_id: str,
    *,
    cc: str,
    phone_number: str,
    verified_name: str,
) -> str | None:
    from core.config import get_settings
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import add_phone_number

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    connection, waba, token = await resolve_waba_and_token(cid, tenant_id)
    if connection is None or waba is None or not token:
        return "No hay conexión/WABA lista todavía"

    try:
        await add_phone_number(
            waba.waba_id,
            token,
            get_settings().meta_app_secret,
            connection.graph_api_version,
            connection.id,
            tenant_id,
            cc=cc,
            phone_number=phone_number,
            verified_name=verified_name,
        )
    except GraphError as exc:
        return exc.message[:140]
    return None


async def request_code(
    tenant_id: uuid.UUID,
    connection_id: str,
    phone_number_id: str,
    *,
    code_method: str,
    language: str = "es",
) -> str | None:
    from core.config import get_settings
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import request_verification_code
    from core.memory.working import get_redis

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return "Conexión inválida"

    connection, _waba, token = await resolve_waba_and_token(cid, tenant_id)
    if connection is None or not token:
        return "No hay conexión lista todavía"

    redis = get_redis()
    # Namespaced por tenant: phone_number_id es único global en Meta, pero un
    # tenant no debe interferir el rate limit de otro (auditoría 2026-09-22).
    rate_limit_key = f"verify_code_resend:{tenant_id}:{phone_number_id}"
    if await redis.get(rate_limit_key):
        return "Esperá un momento antes de reenviar el código"

    try:
        await request_verification_code(
            phone_number_id,
            token,
            get_settings().meta_app_secret,
            connection.graph_api_version,
            connection.id,
            code_method=code_method,
            language=language,
        )
    except GraphError as exc:
        return exc.message[:140]

    await redis.set(rate_limit_key, "1", ex=60)
    return None


async def verify_number(
    tenant_id: uuid.UUID, connection_id: str, phone_number_id: str, *, code: str
) -> tuple[bool, str | None]:
    """(verificado, error)."""
    from core.config import get_settings
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import verify_code

    try:
        cid = uuid.UUID(connection_id)
    except ValueError:
        return False, "Conexión inválida"

    connection, _waba, token = await resolve_waba_and_token(cid, tenant_id)
    if connection is None or not token:
        return False, "No hay conexión lista todavía"

    try:
        ok = await verify_code(
            phone_number_id,
            token,
            get_settings().meta_app_secret,
            connection.graph_api_version,
            connection.id,
            tenant_id,
            code=code,
        )
    except GraphError as exc:
        return False, exc.message[:140]
    return bool(ok), None
