"""ADR-0199 (PR core 5) · medios y teléfono compartido de Telegram contra Postgres real.

- El consumer guarda el teléfono que el cliente compartió de sí mismo en `contact_phone`; repetirlo no cambia nada,
  y un número distinto no pisa el que ya había (conflicto registrado, sin fusionar clientes).
- La ingesta acepta Telegram y descarga con el token del bot de la conexión de la conversación; con la conexión
  pausada no hay credenciales y el mensaje queda como «[Archivo adjunto]» sin llamar a Telegram.

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
from test_telegram_alta_db import BASE, TelegramFalso

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "public_base_url", BASE)
    monkeypatch.setattr(get_settings(), "telegram_habilitado", True)


@pytest.fixture(scope="module")
async def conexion() -> AsyncIterator[dict[str, Any]]:
    from core.alta_negocio import onboard_tenant
    from core.config import get_settings
    from core.gateway.telegram import alta
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"tlm-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Telegram {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    settings = get_settings()
    antes = (settings.public_base_url, settings.telegram_habilitado)
    settings.public_base_url, settings.telegram_habilitado = BASE, True
    try:
        tg = TelegramFalso()
        token = tg.bot()
        async with tenant_scope(tid):
            hecha = await alta.conectar(tid, bot_token=token, transport=tg.transport)
    finally:
        settings.public_base_url, settings.telegram_habilitado = antes
    yield {"tid": tid, "connection_id": hecha.connection_id, "bot_id": hecha.bot_id, "token": token}
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


def _campos(c: dict[str, Any], usuario: str, **extra: str) -> dict[str, str]:
    return {
        "provider": "telegram",
        "channel": "telegram",
        "customer_external_id": usuario,
        "customer_name": "Ana",
        "connection_external_id": c["bot_id"],
        "connection_id": str(c["connection_id"]),
        **extra,
    }


async def _resolver(c: dict[str, Any], campos: dict[str, str]) -> Any:
    from core.orchestrator.consumer import _resolve_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(c["tid"]):
        return await _resolve_conversation(campos)


async def _telefono(c: dict[str, Any], cliente_id: uuid.UUID) -> str | None:
    [(telefono,)] = await _sql(c["tid"], "select contact_phone from customers where id = :i", i=cliente_id)
    return telefono


async def test_el_telefono_propio_se_guarda_sin_pisar_otro(conexion) -> None:
    usuario = str(secrets.randbelow(10**9))
    _c, cliente, _ = await _resolver(conexion, _campos(conexion, usuario, shared_phone="+573001112233"))
    assert await _telefono(conexion, cliente.id) == "+573001112233"

    # Repetirlo no cambia nada; otro número no pisa el guardado (no se fusionan clientes).
    await _resolver(conexion, _campos(conexion, usuario, shared_phone="+573001112233"))
    await _resolver(conexion, _campos(conexion, usuario, shared_phone="+573009998877"))
    assert await _telefono(conexion, cliente.id) == "+573001112233"


async def test_un_telefono_invalido_en_el_stream_no_se_guarda(conexion) -> None:
    _c, cliente, _ = await _resolver(conexion, _campos(conexion, str(secrets.randbelow(10**9)), shared_phone="no-es"))
    assert await _telefono(conexion, cliente.id) is None


async def test_la_ingesta_descarga_con_el_bot_de_la_conversacion(conexion, monkeypatch) -> None:
    from core.media import ingesta, telegram_media
    from core.media.errors import MediaProviderError
    from core.shared.tenant import tenant_scope

    usados: list[tuple[str, str, str]] = []

    async def descargar(file_id: str, bot_token: str, *, mime_esperado: str = "", transport: Any = None):
        usados.append((file_id, bot_token, mime_esperado))
        raise MediaProviderError("simulado", code="media_descarga")

    monkeypatch.setattr(telegram_media, "descargar_media_telegram", descargar)
    conversacion, _cliente, ajustes = await _resolver(conexion, _campos(conexion, str(secrets.randbelow(10**9))))
    campos = {
        **_campos(conexion, "x"),
        "external_message_id": f"tg:{conexion['bot_id']}:in:1:{secrets.randbelow(10**9)}",
        "media": '[{"type": "audio/ogg", "id": "file-123"}]',
        "content": "",
    }
    async with tenant_scope(conexion["tid"]):
        resultado = await ingesta.procesar_media_entrante(campos, conversacion, ajustes)
    assert usados == [("file-123", conexion["token"], "audio/ogg")]
    assert resultado.handled is True

    # Con la conexión pausada no hay credenciales: no se llama a Telegram.
    usados.clear()
    await _sql(conexion["tid"], "update channel_connections set active = false where id = :c", c=conexion["connection_id"])
    try:
        campos["external_message_id"] = f"tg:{conexion['bot_id']}:in:1:{secrets.randbelow(10**9)}"
        async with tenant_scope(conexion["tid"]):
            await ingesta.procesar_media_entrante(campos, conversacion, ajustes)
        assert usados == []
    finally:
        await _sql(conexion["tid"], "update channel_connections set active = true where id = :c", c=conexion["connection_id"])
