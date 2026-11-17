"""La pausa del negocio y del cliente frena lo automático hacia el cliente (HU-ARCH-37, D-76), contra la base.

- **Seguimientos:** con el negocio A pausado no sale el seguimiento, queda `failed negocio_pausado` y el lead sigue
  `calificado` (no se gasta la marca); el negocio B, activo, envía.
- **Al reanudar:** el lead vuelve a ser candidato y sale.
- **Ya en cola:** un mensaje del agente encolado antes de pausar falla con `negocio_pausado` sin reintentos; el de
  una persona no lo frena la pausa.
- **Avisos por evento:** con A pausado el aviso no sale ni se reintenta; en B sale.
- **Cliente pausado:** frena solo a ese cliente.

Sin LLM: los seguimientos y los avisos no llaman al modelo.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from _api_helpers import uniq
from test_kernel_ingress import _ajustes, _filas, _negocio
from test_pausa_conexion_propia import _conexion, _mensajes, _sin_cola

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # la pausa de un negocio no toca a otro
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CONFIG = SimpleNamespace(follow_up_hours=1, follow_up_lead_message="Hola{name}, ¿seguimos con {interest}?")


_CONEXIONES: dict[uuid.UUID, str] = {}


async def _conversacion(tid: uuid.UUID) -> uuid.UUID:
    """Una conversación nueva (otro cliente) en la conexión de WhatsApp Web del negocio (una sola viva por negocio)."""
    from test_conexion_canonica import _entrada

    if tid not in _CONEXIONES:
        _cid, _CONEXIONES[tid] = await _conexion(tid, "waweb")
    return (await _entrada(tid, uniq("wa-pausa"), _CONEXIONES[tid])).id


async def _lead(tid: uuid.UUID, conv: uuid.UUID) -> uuid.UUID:
    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            lead = Lead(
                tenant_id=tid, conversation_id=conv, name="Ana", interest="tortas", status="calificado",
                last_activity_at=datetime.now(UTC) - timedelta(hours=2),
            )
            session.add(lead)
            await session.flush()
            return lead.id


async def _etapa(tid: uuid.UUID, lead: uuid.UUID) -> str:
    (fila,) = await _filas(tid, "SELECT status FROM pack_commerce_leads WHERE id = :l", l=lead)
    return fila["status"]


async def _seguimiento(tid: uuid.UUID) -> tuple[int, int]:
    from pack_commerce.jobs import _follow_up_leads_del_negocio

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await _follow_up_leads_del_negocio(tid, CONFIG)


async def test_seguimiento_frenado_en_a_sale_en_b_y_al_reanudar(monkeypatch) -> None:
    encolados = _sin_cola(monkeypatch)
    tid_a, tid_b = await _negocio(), await _negocio()
    conv_a, conv_b = await _conversacion(tid_a), await _conversacion(tid_b)
    lead_a, lead_b = await _lead(tid_a, conv_a), await _lead(tid_b, conv_b)
    await _ajustes(tid_a, bot_paused=True)

    assert await _seguimiento(tid_a) == (1, 0)
    assert await _etapa(tid_a, lead_a) == "calificado", "no se gasta la marca: sigue siendo candidato"
    assert [m for m in await _mensajes(tid_a, conv_a) if m[0] == "outbound"] == [
        ("outbound", "failed", "negocio_pausado")
    ]

    assert await _seguimiento(tid_b) == (1, 1), "el negocio B no se entera de la pausa de A"
    assert await _etapa(tid_b, lead_b) == "contactado"
    assert len(encolados) == 1

    await _ajustes(tid_a, bot_paused=False)
    assert await _seguimiento(tid_a) == (1, 1), "al reanudar vuelve a ser candidato y sale"
    assert await _etapa(tid_a, lead_a) == "contactado"
    assert len(encolados) == 2
    salientes = [m for m in await _mensajes(tid_a, conv_a) if m[0] == "outbound"]
    assert salientes[0] == ("outbound", "failed", "negocio_pausado"), "lo frenado no se reenvía"


async def test_cliente_pausado_frena_solo_a_ese_cliente(monkeypatch) -> None:
    from sqlalchemy import update

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.gateway.sender import send_to_conversation
    from core.shared.tenant import tenant_scope

    encolados = _sin_cola(monkeypatch)
    tid = await _negocio()
    pausada, activa = await _conversacion(tid), await _conversacion(tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = (await session.get(Conversation, pausada)).customer_id
            await session.execute(update(Customer).where(Customer.id == cliente).values(bot_paused=True))
        assert await send_to_conversation(pausada, "Hola") is False
        assert await send_to_conversation(activa, "Hola") is True
        assert await send_to_conversation(pausada, "Te escribe Ana del equipo", sender_type="human") is True
    assert len(encolados) == 2
    assert ("outbound", "failed", "cliente_pausado") in await _mensajes(tid, pausada)


async def test_lo_encolado_del_agente_falla_al_pausar_y_lo_de_una_persona_no(monkeypatch) -> None:
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks import outbound_send

    tid = await _negocio()
    conv = await _conversacion(tid)
    ids: dict[str, uuid.UUID] = {}
    async with tenant_scope(tid):
        async with get_session() as session:
            for quien in ("agent", "human"):
                m = Message(
                    tenant_id=tid, conversation_id=conv, direction="outbound", sender_type=quien,
                    content=f"de {quien}", status="queued",
                )
                session.add(m)
                await session.flush()
                ids[quien] = m.id
    await _ajustes(tid, bot_paused=True)

    error = await outbound_send._send_one(ids["agent"], tid)
    assert isinstance(error, outbound_send.PausaAutomaticaError)
    assert str(error) == "negocio_pausado" or "negocio_pausado" in getattr(error, "message", str(error))
    assert outbound_send.decide_retry(error, attempts_made=0).action == "fail_permanent"

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.delete(await session.get(Message, ids["agent"]))
    otro = await outbound_send._send_one(ids["human"], tid)
    assert not isinstance(otro, outbound_send.PausaAutomaticaError), "lo de una persona no lo frena la pausa"


async def test_aviso_por_evento_no_sale_en_a_y_sale_en_b(monkeypatch) -> None:
    from core.gateway.event_notices import send_event_notice
    from core.shared.tenant import tenant_scope

    _sin_cola(monkeypatch)
    tid_a, tid_b = await _negocio(), await _negocio()
    conv_a, conv_b = await _conversacion(tid_a), await _conversacion(tid_b)
    await _ajustes(tid_a, bot_paused=True)
    contexto = {"cliente": {"nombre": "Ana"}}

    async with tenant_scope(tid_a):
        res_a = await send_event_notice(
            conv_a, "appointment.reminder", contexto, notice_key=uniq("cita"), default_fallback_text="Te esperamos mañana."
        )
    async with tenant_scope(tid_b):
        res_b = await send_event_notice(
            conv_b, "appointment.reminder", contexto, notice_key=uniq("cita"), default_fallback_text="Te esperamos mañana."
        )

    assert res_a.modo == "negocio_pausado"
    assert ("outbound", "failed", "negocio_pausado") in await _mensajes(tid_a, conv_a)
    assert res_b.modo not in ("negocio_pausado", "cliente_pausado")
