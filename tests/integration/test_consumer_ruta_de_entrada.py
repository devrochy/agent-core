"""La conversación responde por donde el cliente escribió la última vez.

Visto en test (2026-10-08): un cliente que había escrito por WhatsApp Web (QR) le escribió al
número de Meta del mismo negocio. El mensaje entró a la conversación abierta, que quedó con las
DOS conexiones (`connection_id` de WhatsApp Web y `meta_connection_id`); `resolve_credentials`
prioriza `connection_id`, así que la respuesta salió por WhatsApp Web (sesión caída:
`client is nil`) y nunca llegó.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from test_api_registrar_numero import _negocio_con_byok

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _waweb(tid: uuid.UUID) -> tuple[uuid.UUID, str]:
    """Conexión de WhatsApp Web (QR) del negocio, como la crea el conector."""
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    external_id = f"waweb:{tid}"
    async with tenant_scope(tid):
        async with get_session() as session:
            conexion = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="waweb", label="WhatsApp (vínculo por QR)",
                active=True, is_primary=False, external_id=external_id,
            )
            session.add(conexion)
            await session.flush()
            return conexion.id, external_id


async def _entrada(tid: uuid.UUID, cliente: str, por: str):
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        conversacion, _, _ = await consumer._resolve_conversation(
            {"channel": "whatsapp", "customer_external_id": cliente, "connection_external_id": por}
        )
    return conversacion


async def test_de_whatsapp_web_a_meta_es_otra_conversacion_por_meta() -> None:
    """Desde ADR-0191 la conversación es por (conexión, cliente): la entrada por Meta abre la suya y
    responde por Meta; la de WhatsApp Web no se toca (nunca queda con dos rutas)."""
    tid, meta_cid, _waba, phone_id = await _negocio_con_byok()
    waweb_id, waweb_ext = await _waweb(tid)
    cliente = uniq("wa-ruta")

    primera = await _entrada(tid, cliente, waweb_ext)
    assert (primera.connection_id, primera.meta_connection_id) == (waweb_id, None)

    segunda = await _entrada(tid, cliente, phone_id)
    assert segunda.id != primera.id
    assert segunda.connection_id not in (None, waweb_id) and segunda.meta_connection_id == meta_cid
    assert (await _entrada(tid, cliente, waweb_ext)).id == primera.id  # la de WhatsApp Web sigue siendo la suya


async def test_de_meta_a_whatsapp_web_es_otra_conversacion_por_whatsapp_web() -> None:
    tid, meta_cid, _waba, phone_id = await _negocio_con_byok()
    waweb_id, waweb_ext = await _waweb(tid)
    cliente = uniq("wa-ruta")

    primera = await _entrada(tid, cliente, phone_id)
    assert primera.meta_connection_id == meta_cid and primera.connection_id is not None

    segunda = await _entrada(tid, cliente, waweb_ext)
    assert segunda.id != primera.id
    assert (segunda.connection_id, segunda.meta_connection_id) == (waweb_id, None)


async def test_entrada_sin_ruta_conocida_no_reescribe_la_de_una_conexion() -> None:
    """Sin conexión identificable (p. ej. el número de la plataforma por la configuración) no se
    toca la conversación de una conexión: su ruta sigue igual."""
    tid, meta_cid, _waba, phone_id = await _negocio_con_byok()
    cliente = uniq("wa-ruta")

    primera = await _entrada(tid, cliente, phone_id)
    segunda = await _entrada(tid, cliente, "numero-desconocido")
    assert segunda.id != primera.id
    otra_vez = await _entrada(tid, cliente, phone_id)
    assert otra_vez.id == primera.id
    assert (otra_vez.connection_id, otra_vez.meta_connection_id) == (primera.connection_id, meta_cid)
