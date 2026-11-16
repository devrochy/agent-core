"""Interruptor global de la plataforma contra la base (HU-ARCH-38, ADR-0200).

- **Apagado global:** con el interruptor activo, lo que entra a cualquier negocio se guarda, no corre el LLM y
  queda `kill_switch` en `core_decisions`; activarlo frena turnos nuevos en ≤ `TTL_S`.
- **También lo proactivo:** un mensaje automático en cola falla con `kill_switch`; uno de una persona no.
- **Persona:** un operador responde y sale.
- **Reactivar sin avalancha:** la recuperación no reprograma lo que llegó con el interruptor activo.
- **Historial:** cada cambio queda con quién, cuándo y por qué.

Sin LLM real: el proveedor del turno falla si lo llaman.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from test_kernel_ingress import _filas, _inmediato, _negocio, _sin_llm, _turnos
from test_pausa_conexion_propia import _conexion, _entrar, _sin_cola

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # el interruptor es de la plataforma: frena a todos los negocios a la vez
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


@pytest.fixture
async def interruptor():
    """Activa y apaga el interruptor por su servicio; siempre lo deja apagado al terminar."""
    from core.admin import kill_switch_service as svc

    async def _fijar(activo: bool) -> None:
        await svc.fijar(activo, "ensayo de integración" if activo else None, admin_id=None, etiqueta="test", fuente="cli")

    yield _fijar
    await _fijar(False)


async def test_con_el_interruptor_lo_que_entra_se_guarda_sin_llm_en_todos_los_negocios(monkeypatch, interruptor) -> None:
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)
    _sin_cola(monkeypatch)
    tid_a, tid_b = await _negocio(), await _negocio()
    _cid_a, ext_a = await _conexion(tid_a, "waweb")
    _cid_b, ext_b = await _conexion(tid_b, "waweb")
    await interruptor(True)

    conv_a = await _entrar(tid_a, uniq("wa-ks-a"), ext_a, "waweb")
    conv_b = await _entrar(tid_b, uniq("wa-ks-b"), ext_b, "waweb")

    for tid, conv in ((tid_a, conv_a), (tid_b, conv_b)):
        assert await _turnos(tid, conv) == [("blocked", "kill_switch", "silent")]
        direcciones = [f["direction"] for f in await _filas(tid, "SELECT direction FROM messages WHERE conversation_id = :c", c=conv)]
        assert direcciones == ["inbound"], "el mensaje queda en la bandeja y no se responde"


async def test_lo_automatico_en_cola_falla_y_lo_de_una_persona_no(monkeypatch, interruptor) -> None:
    from test_conexion_canonica import _entrada

    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks import outbound_send

    tid = await _negocio()
    _cid, external_id = await _conexion(tid, "manual")
    conv = (await _entrada(tid, uniq("wa-ks-cola"), external_id)).id
    ids: dict[str, uuid.UUID] = {}
    async with tenant_scope(tid):
        async with get_session() as session:
            for quien in ("agent", "human"):
                m = Message(tenant_id=tid, conversation_id=conv, direction="outbound", sender_type=quien,
                            content=f"de {quien}", status="queued")
                session.add(m)
                await session.flush()
                ids[quien] = m.id
    await interruptor(True)

    error = await outbound_send._send_one(ids["agent"], tid)
    assert isinstance(error, outbound_send.PausaAutomaticaError)
    assert "kill_switch" in str(error)

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.delete(await session.get(Message, ids["agent"]))
    otro = await outbound_send._send_one(ids["human"], tid)
    assert not isinstance(otro, outbound_send.PausaAutomaticaError), "lo de una persona sale"


async def test_un_operador_responde_con_el_interruptor_activo(monkeypatch, interruptor) -> None:
    from test_conexion_canonica import _entrada

    from core.gateway.sender import send_to_conversation
    from core.shared.tenant import tenant_scope

    encolados = _sin_cola(monkeypatch)
    tid = await _negocio()
    _cid, external_id = await _conexion(tid, "waweb")
    conv = (await _entrada(tid, uniq("wa-ks-op"), external_id)).id
    await interruptor(True)
    async with tenant_scope(tid):
        assert await send_to_conversation(conv, "Respuesta automática") is False
        assert await send_to_conversation(conv, "Te escribe Ana", sender_type="human") is True
    assert len(encolados) == 1
    filas = await _filas(tid, "SELECT status, error_reason FROM messages WHERE conversation_id = :c AND direction = 'outbound'", c=conv)
    assert {"status": "failed", "error_reason": "kill_switch"} in filas


async def test_reactivar_no_responde_en_lote_lo_que_llego_durante_el_apagado(monkeypatch, interruptor) -> None:
    from core.orchestrator import debounce

    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)
    _sin_cola(monkeypatch)
    programadas: list[uuid.UUID] = []

    async def _programar(conversation_id, *_a, **_k):
        programadas.append(conversation_id)

    tid = await _negocio()
    _cid, external_id = await _conexion(tid, "waweb")
    await interruptor(True)
    conv = await _entrar(tid, uniq("wa-ks-rec"), external_id, "waweb")

    monkeypatch.setattr(debounce, "schedule_debounce", _programar)
    await debounce.recover_orphaned_conversations()
    assert conv not in programadas, "con el interruptor activo no se recupera nada"

    await interruptor(False)
    await debounce.recover_orphaned_conversations()
    assert conv not in programadas, "lo que llegó durante el apagado queda en la bandeja"


async def test_cada_cambio_queda_en_el_historial(interruptor) -> None:
    from core.admin import kill_switch_service as svc

    await interruptor(True)
    await interruptor(False)
    estado = await svc.estado()
    assert estado.active is False
    assert [e.action for e in estado.history[:2]] == ["deactivated", "activated"]
    assert estado.history[1].reason == "ensayo de integración"
    assert estado.history[1].actor_label == "test"
