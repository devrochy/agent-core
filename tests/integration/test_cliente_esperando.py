"""Aviso «cliente esperando respuesta» (ADR-0167): un cliente con una escalación abierta que
lleva más de 15 min sin respuesta humana genera un aviso por episodio. Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AHORA = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)


def _hace(minutos: int) -> datetime:
    return AHORA - timedelta(minutes=minutos)


async def _escalada(
    *, hace: int, estado: str = "pendiente", admin_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Negocio con una conversación escalada hace `hace` minutos. Devuelve (tenant, conv, escalación)."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import Conversation, Customer, Escalation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("espera")
    tid, _ = await onboard_tenant(
        name=f"Espera {slug}", slug=slug, business_description="Prueba.", admin_email=f"{slug}@test.local",
        admin_password="password123", packs=[],
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Ana")
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id, status="escalated", bot_paused=True)
            session.add(conv)
            await session.flush()
            esc = Escalation(
                tenant_id=tid, conversation_id=conv.id, reason="tool:queja", summary="x", status=estado,
                created_at=_hace(hace), assigned_admin_id=admin_id,
            )
            session.add(esc)
            await session.flush()
            conv_id, esc_id = conv.id, esc.id
    return tid, conv_id, esc_id


async def _mensaje(tid: uuid.UUID, conv: uuid.UUID, *, direccion: str, hace: int) -> None:
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=tid, conversation_id=conv, direction=direccion,
                    sender_type="customer" if direccion == "inbound" else "human",
                    content="hola", created_at=_hace(hace),
                )
            )


async def _avisos(tid: uuid.UUID):
    from core.db.models import Notification
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(
                    select(Notification).where(Notification.event_key == "core.customer_waiting")
                )
            ).scalars().all()


async def _correr() -> None:
    from core.tasks.cliente_esperando import avisar_clientes_esperando

    await avisar_clientes_esperando(ahora=AHORA)


async def test_avisa_una_vez_al_pasar_el_umbral_sin_respuesta_humana(app_client) -> None:
    tid, conv, esc = await _escalada(hace=30)
    await _correr()
    await _correr()  # otra pasada del beat: mismo episodio, sin repetir

    avisos = await _avisos(tid)
    assert len(avisos) == 1
    aviso = avisos[0]
    assert (aviso.category, aviso.severity, aviso.section) == ("conversaciones", "warning", "escalations")
    assert aviso.target == {"entity": "conversacion", "id": str(conv), "section": "escalations"}
    assert aviso.data["escalationId"] == str(esc) and "15 minutos" in aviso.body


async def test_no_avisa_antes_del_umbral_ni_de_escalaciones_resueltas_o_abandonadas(app_client) -> None:
    reciente, _, _ = await _escalada(hace=5)
    resuelta, _, _ = await _escalada(hace=30, estado="resuelta")
    abandonada, _, _ = await _escalada(hace=3 * 24 * 60)
    await _correr()
    assert await _avisos(reciente) == []
    assert await _avisos(resuelta) == []
    assert await _avisos(abandonada) == []


async def test_una_respuesta_humana_corta_la_espera_y_un_mensaje_nuevo_abre_otro_episodio(app_client) -> None:
    tid, conv, _ = await _escalada(hace=60)
    await _mensaje(tid, conv, direccion="outbound", hace=40)  # el equipo respondió
    await _correr()
    assert await _avisos(tid) == []  # nadie espera

    await _mensaje(tid, conv, direccion="inbound", hace=20)  # el cliente vuelve a escribir
    await _correr()
    assert len(await _avisos(tid)) == 1

    await _mensaje(tid, conv, direccion="inbound", hace=18)  # otro mensaje del mismo episodio
    await _correr()
    assert len(await _avisos(tid)) == 1


async def test_asignada_va_dirigida_a_la_persona(app_client) -> None:
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        persona = Admin(email=f"{uniq('asig')}@test.local", password_hash="x")
        session.add(persona)
        await session.flush()
        persona_id = persona.id
    tid, _, _ = await _escalada(hace=45, estado="asignada", admin_id=persona_id)
    await _correr()
    avisos = await _avisos(tid)
    assert len(avisos) == 1 and avisos[0].audience_admin_id == persona_id
