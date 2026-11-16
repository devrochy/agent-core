"""ADR-0199 · esquema del canal Telegram (migración 0137) contra Postgres real.

- RLS forzada: un negocio no ve ni escribe la fila `telegram_connections` de otro.
- Un bot, un negocio: `uq_channel_connections_telegram_bot_viva` rechaza el mismo `bot_id` vivo en otro negocio; tras
  el borrado lógico, el bot se puede conectar en otro negocio.
- 1:1: una sola fila viva por conexión.
- `telegram_route_connection(public_id)` resuelve el negocio sin `tenant_scope` y no devuelve conexiones borradas.
- CHECKs de estado y de `public_id`.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _bot_id() -> str:
    return str(secrets.randbelow(10**10) + 10**9)


def _token(bot_id: str) -> str:
    return f"{bot_id}:AAH" + secrets.token_urlsafe(24)


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados: list[tuple[uuid.UUID, str]] = []
    for prefijo in ("tga", "tgb"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Telegram {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        creados.append((tid, slug))
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
        async with tenant_scope(tid):
            await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if resultado.returns_rows else []


async def _conexion(tid: uuid.UUID, bot_id: str) -> uuid.UUID:
    """Una `channel_connections` de Telegram como la guardará el alta: `external_id` = `bot_id` de `getMe`."""
    from core.db.session import get_session
    from core.gateway.connection_types import TELEGRAM
    from core.gateway.connections import save_connection_in
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            connection, _ = await save_connection_in(
                session,
                "Bot",
                {"bot_token": _token(bot_id), "bot_id": bot_id, "webhook_secret": secrets.token_urlsafe(32)},
                type_id=TELEGRAM,
            )
            await session.flush()
            return connection.id


async def _fila(tid: uuid.UUID, connection_id: uuid.UUID, **campos: Any) -> str:
    from core.db.models import TelegramConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    public_id = campos.pop("public_id", secrets.token_urlsafe(18))
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                TelegramConnection(
                    tenant_id=tid,
                    channel_connection_id=connection_id,
                    public_id=public_id,
                    bot_username="mi_bot",
                    **campos,
                )
            )
    return public_id


def _restriccion(exc: pytest.ExceptionInfo[Any]) -> str:
    import re

    hallado = re.search(r'constraint "([a-z0-9_]+)"', str(exc.value.orig))
    return hallado.group(1) if hallado else str(exc.value.orig)


async def test_la_conexion_guarda_el_bot_id_y_las_credenciales_cifradas(negocios) -> None:
    tid, _ = negocios
    bot = _bot_id()
    cid = await _conexion(tid, bot)
    [(external_id, provider, channel, type_id, cifradas)] = await _sql(
        tid,
        "select external_id, provider, channel, type_id, credentials_encrypted from channel_connections where id = :c",
        c=cid,
    )
    assert (external_id, provider, channel, type_id) == (bot, "telegram", "telegram", "telegram")
    assert cifradas and ":AAH" not in cifradas, "el token no queda en claro"


async def test_un_bot_vivo_en_un_solo_negocio_y_el_borrado_lo_libera(negocios) -> None:
    a, b = negocios
    bot = _bot_id()
    cid = await _conexion(a, bot)
    with pytest.raises(IntegrityError) as exc:
        await _conexion(b, bot)
    assert _restriccion(exc) == "uq_channel_connections_telegram_bot_viva"

    await _sql(a, "update channel_connections set deleted_at = now() where id = :c", c=cid)
    otra = await _conexion(b, bot)
    assert otra != cid


async def test_rls_un_negocio_no_ve_ni_escribe_la_fila_de_otro(negocios) -> None:
    a, b = negocios
    cid = await _conexion(a, _bot_id())
    public_id = await _fila(a, cid)

    assert await _sql(a, "select 1 from telegram_connections where public_id = :p", p=public_id)
    assert await _sql(b, "select 1 from telegram_connections where public_id = :p", p=public_id) == []
    assert await _sql(None, "select 1 from telegram_connections where public_id = :p", p=public_id) == []
    await _sql(b, "update telegram_connections set status = 'error' where public_id = :p", p=public_id)
    [(estado,)] = await _sql(a, "select status from telegram_connections where public_id = :p", p=public_id)
    assert estado == "pending"
    # Escribir una fila con el `tenant_id` de otro negocio también se rechaza (WITH CHECK y FK compuesta).
    with pytest.raises(DBAPIError):
        await _fila(b, cid)


async def test_una_fila_viva_por_conexion(negocios) -> None:
    tid, _ = negocios
    cid = await _conexion(tid, _bot_id())
    await _fila(tid, cid)
    with pytest.raises(IntegrityError) as exc:
        await _fila(tid, cid)
    assert _restriccion(exc) == "uq_telegram_connections_conexion_viva"


@pytest.mark.parametrize(
    ("campos", "restriccion"),
    [
        ({"status": "activo"}, "ck_telegram_connections_status"),
        ({"public_id": "corto"}, "ck_telegram_connections_public_id"),
        ({"pending_update_count": -1}, "ck_telegram_connections_pending_update_count"),
    ],
)
async def test_checks(negocios, campos, restriccion) -> None:
    tid, _ = negocios
    cid = await _conexion(tid, _bot_id())
    with pytest.raises(IntegrityError) as exc:
        await _fila(tid, cid, **campos)
    assert _restriccion(exc) == restriccion


async def test_route_connection_resuelve_sin_negocio_y_no_las_borradas(negocios) -> None:
    tid, _ = negocios
    cid = await _conexion(tid, _bot_id())
    public_id = await _fila(tid, cid)

    [(tenant_id, conexion)] = await _sql(None, "select * from telegram_route_connection(:p)", p=public_id)
    assert (tenant_id, conexion) == (tid, cid)
    assert await _sql(None, "select * from telegram_route_connection(:p)", p="no-existe-" + "x" * 10) == []

    await _sql(tid, "update channel_connections set deleted_at = now() where id = :c", c=cid)
    assert await _sql(None, "select * from telegram_route_connection(:p)", p=public_id) == []
