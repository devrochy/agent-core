"""HU-CONN-20 (adenda del ADR-0186) · esquema del modo Sandbox de Twilio (migración 0136) contra Postgres real.

- `modo` por defecto es `sender`; el CHECK exige `sender_sid` en `sender` y lo prohíbe en `sandbox`.
- `sandbox_join_code` solo en modo sandbox y con formato acotado.
- Un sandbox vivo por cuenta en cada negocio (índice parcial); uno borrado no estorba.
- RLS: un negocio no ve la fila sandbox de otro.
- `save_connection_in(external_id_override=…)` guarda `sandbox:<AccountSid>` y el alta repetida encuentra la misma
  conexión; el mismo override en otro negocio choca con `uq_channel_connections_twilio_sender_viva`.

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
from sqlalchemy.exc import IntegrityError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SANDBOX = "+14155238886"
XE = "XE" + "2" * 32


def _cuenta() -> str:
    return "AC" + uuid.uuid4().hex


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from _habilidades import onboard_tenant

    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados: list[tuple[uuid.UUID, str]] = []
    for prefijo in ("tsa", "tsb"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(
            name=f"Sandbox {slug}", slug=slug, packs=[], sembrar_ejemplos=False
        )
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


async def _conexion(tid: uuid.UUID, account_sid: str) -> tuple[uuid.UUID, bool]:
    """Una `channel_connections` de Twilio con el `external_id` del sandbox, como la guardará el alta."""
    from core.db.session import get_session
    from core.gateway.connection_types import WHATSAPP_TWILIO
    from core.gateway.connections import save_connection_in
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            connection, created = await save_connection_in(
                session,
                "Sandbox",
                {"account_sid": account_sid, "auth_token": "token", "sender_e164": SANDBOX},
                type_id=WHATSAPP_TWILIO,
                external_id_override=f"sandbox:{account_sid}",
            )
            await session.flush()
            return connection.id, created


async def _fila(
    tid: uuid.UUID,
    connection_id: uuid.UUID,
    account_sid: str,
    *,
    modo: str = "sandbox",
    sender_sid: str | None = None,
    join_code: str | None = None,
) -> None:
    from core.db.models import TwilioConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                TwilioConnection(
                    tenant_id=tid,
                    channel_connection_id=connection_id,
                    public_id=secrets.token_urlsafe(18),
                    account_sid=account_sid,
                    modo=modo,
                    sender_sid=sender_sid,
                    sender_e164=SANDBOX,
                    sandbox_join_code=join_code,
                    webhook_inbound_url="https://x/in",
                    webhook_status_url="https://x/st",
                )
            )


def _restriccion(exc: pytest.ExceptionInfo[IntegrityError]) -> str:
    """Nombre del CHECK o índice que rechazó la fila (asyncpg lo deja en el texto del error)."""
    import re

    hallado = re.search(r'constraint "([a-z0-9_]+)"', str(exc.value.orig))
    return hallado.group(1) if hallado else str(exc.value.orig)


async def test_override_guarda_external_id_del_sandbox_y_es_idempotente(negocios) -> None:
    tid, _ = negocios
    ac = _cuenta()
    primera, creada = await _conexion(tid, ac)
    segunda, otra_vez = await _conexion(tid, ac)

    assert creada is True and otra_vez is False and segunda == primera
    [(external_id, provider)] = await _sql(
        tid, "select external_id, provider from channel_connections where id = :c", c=primera
    )
    assert (external_id, provider) == (f"sandbox:{ac}", "twilio")


async def test_el_mismo_sandbox_en_otro_negocio_choca_con_el_indice_global(negocios) -> None:
    tid_a, tid_b = negocios
    ac = _cuenta()
    await _conexion(tid_a, ac)
    with pytest.raises(IntegrityError) as exc:
        await _conexion(tid_b, ac)
    assert "uq_channel_connections_twilio_sender_viva" in str(exc.value.orig)


async def test_checks_de_modo_sender_sid_y_join_code(negocios) -> None:
    tid, _ = negocios
    ac = _cuenta()
    connection_id, _ = await _conexion(tid, ac)

    with pytest.raises(IntegrityError) as exc:
        await _fila(tid, connection_id, ac, sender_sid=XE)
    assert _restriccion(exc) == "ck_twilio_connections_sender_sid_por_modo"

    with pytest.raises(IntegrityError) as exc:
        await _fila(tid, connection_id, ac, modo="sender", sender_sid=None)
    assert _restriccion(exc) == "ck_twilio_connections_sender_sid_por_modo"

    with pytest.raises(IntegrityError) as exc:
        await _fila(
            tid, connection_id, ac, modo="sender", sender_sid=XE, join_code="cloud-forgotten"
        )
    assert _restriccion(exc) == "ck_twilio_connections_sandbox_join_code"

    for malo in ("Cloud Forgotten", "join cloud", "-x", "a" * 65):
        with pytest.raises(IntegrityError) as exc:
            await _fila(tid, connection_id, ac, join_code=malo)
        assert _restriccion(exc) == "ck_twilio_connections_sandbox_join_code", malo

    with pytest.raises(IntegrityError) as exc:
        await _fila(tid, connection_id, ac, modo="otro", sender_sid=XE)
    assert _restriccion(exc) in (
        "ck_twilio_connections_modo",
        "ck_twilio_connections_sender_sid_por_modo",
    )

    await _fila(tid, connection_id, ac, join_code="cloud-forgotten")
    [(modo, sender_sid, codigo, confirmado)] = await _sql(
        tid,
        "select modo, sender_sid, sandbox_join_code, webhook_confirmado_at from twilio_connections "
        "where channel_connection_id = :c",
        c=connection_id,
    )
    assert (modo, sender_sid, codigo, confirmado) == ("sandbox", None, "cloud-forgotten", None)


async def test_modo_por_defecto_es_sender(negocios) -> None:
    tid, _ = negocios
    from core.db.session import get_session
    from core.gateway.connection_types import WHATSAPP_TWILIO
    from core.gateway.connections import save_connection_in
    from core.shared.tenant import tenant_scope

    e164 = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    async with tenant_scope(tid):
        async with get_session() as session:
            connection, _ = await save_connection_in(
                session,
                "Sender",
                {"account_sid": _cuenta(), "auth_token": "t", "sender_e164": e164},
                type_id=WHATSAPP_TWILIO,
            )
            await session.flush()
            await session.execute(
                text(
                    "insert into twilio_connections (id, tenant_id, channel_connection_id, public_id, account_sid, "
                    "sender_sid, sender_e164, webhook_inbound_url, webhook_status_url) values (gen_random_uuid(), "
                    ":t, :c, :p, :a, :s, :e, 'https://x/in', 'https://x/st')"
                ),
                {
                    "t": tid,
                    "c": connection.id,
                    "p": secrets.token_urlsafe(18),
                    "a": _cuenta(),
                    "s": XE,
                    "e": e164,
                },
            )
            connection_id = connection.id
    [(modo, external_id)] = await _sql(
        tid,
        "select tc.modo, cc.external_id from twilio_connections tc join channel_connections cc "
        "on cc.id = tc.channel_connection_id where tc.channel_connection_id = :c",
        c=connection_id,
    )
    assert (modo, external_id) == ("sender", e164)


async def test_un_sandbox_vivo_por_cuenta_y_rls(negocios) -> None:
    tid_a, tid_b = negocios
    ac = _cuenta()
    primera, _ = await _conexion(tid_a, ac)
    await _fila(tid_a, primera, ac)

    # Otra conexión del mismo negocio con la misma cuenta en modo sandbox: el índice parcial lo impide.
    otra, _ = await _conexion(tid_a, _cuenta())
    with pytest.raises(IntegrityError) as exc:
        await _fila(tid_a, otra, ac)
    assert "uq_twilio_connections_sandbox_cuenta_viva" in str(exc.value.orig)

    # Borrada lógicamente, ya no cuenta para el índice.
    await _sql(
        tid_a,
        "update twilio_connections set deleted_at = now() where channel_connection_id = :c",
        c=primera,
    )
    await _fila(tid_a, otra, ac)

    # RLS: B no ve los sandbox de A; sin negocio, nada.
    assert await _sql(tid_b, "select 1 from twilio_connections where account_sid = :a", a=ac) == []
    assert await _sql(None, "select 1 from twilio_connections where account_sid = :a", a=ac) == []
    assert (
        len(await _sql(tid_a, "select 1 from twilio_connections where account_sid = :a", a=ac)) == 2
    )


async def test_el_override_solo_vale_para_la_cuenta_de_las_credenciales(negocios) -> None:
    tid, _ = negocios
    from core.db.session import get_session
    from core.gateway.connection_types import WHATSAPP_META, WHATSAPP_TWILIO
    from core.gateway.connections import save_connection_in
    from core.shared.tenant import tenant_scope

    ajena = _cuenta()
    casos = [
        (WHATSAPP_TWILIO, f"sandbox:{ajena}"),  # la cuenta de otro, con mis credenciales
        (WHATSAPP_TWILIO, "+14155238886"),
        (WHATSAPP_META, "sandbox:x"),
    ]
    for type_id, override in casos:
        async with tenant_scope(tid):
            async with get_session() as session:
                with pytest.raises(ValueError):
                    await save_connection_in(
                        session,
                        "x",
                        {"account_sid": _cuenta(), "auth_token": "t", "sender_e164": SANDBOX},
                        type_id=type_id,
                        external_id_override=override,
                    )
    assert (
        await _sql(
            tid, "select 1 from channel_connections where external_id = :e", e=f"sandbox:{ajena}"
        )
        == []
    )


async def test_editar_un_sandbox_sin_override_conserva_su_external_id(negocios) -> None:
    tid, _ = negocios
    from core.db.session import get_session
    from core.gateway.connection_types import WHATSAPP_TWILIO
    from core.gateway.connections import save_connection_in
    from core.shared.tenant import tenant_scope

    ac = _cuenta()
    connection_id, _ = await _conexion(tid, ac)
    async with tenant_scope(tid):
        async with get_session() as session:
            await save_connection_in(
                session,
                "Otra etiqueta",
                {"account_sid": ac, "auth_token": "nuevo", "sender_e164": SANDBOX},
                connection_id=connection_id,
                type_id=WHATSAPP_TWILIO,
            )
    [(external_id, label)] = await _sql(
        tid, "select external_id, label from channel_connections where id = :c", c=connection_id
    )
    assert (external_id, label) == (f"sandbox:{ac}", "Otra etiqueta")
