"""Pausar una conexión sin cuenta de Meta la detiene (HU-AOS-23, #472; adenda 2026-10-19 de ADR-0122).

- **Entrada:** con una conexión manual o de WhatsApp Web pausada (`active = false`), lo que entra se
  guarda, no se llama al LLM y queda `connection_paused` en `core_decisions`, con un solo push
  «Conexión pausada» (`scope = "connection"`).
- **Salida:** ni un seguimiento de pack-commerce ni lo encolado antes de pausar salen por ella.
- **Reanudar:** al reactivarla, el turno vuelve a correr.
- **Aislamiento:** pausar la conexión de un negocio no toca la de otro.
- **Sin estado propio en Meta:** la fila canónica de un número de Meta con `active = false` no pausa
  (manda la cuenta, ADR-0115 §2); una manual inactiva que choca con una cuenta viva sí
  (`choque_manual`, documentado).

El LLM es el `FakeProvider` del conftest o uno que falla si lo llaman: nunca hay LLM real.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from _api_helpers import uniq
from test_kernel_ingress import _filas, _inmediato, _negocio, _sin_llm, _turnos

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # guarda de pausa por conexión y aislamiento entre negocios
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


# ---------------------------------------------------------------- helpers
async def _conexion(tid: uuid.UUID, tipo: str) -> tuple[uuid.UUID, str]:
    """Una conexión sin cuenta de Meta: `manual` (WhatsApp con credenciales propias) o `waweb`."""
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.gateway.connection_types import CATEGORY_COMMUNICATION, WHATSAPP_META
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope
    from core.waweb.connections import WAWEB_PROVIDER, WAWEB_TYPE_ID

    external_id = uniq(f"ext-{tipo}")
    if tipo == "manual":
        provider, type_id = "meta", WHATSAPP_META
        creds = get_secret_box().encrypt_json({"phone_number_id": external_id, "access_token": "tok-manual"})
    else:
        provider, type_id, creds = WAWEB_PROVIDER, WAWEB_TYPE_ID, None
    async with tenant_scope(tid):
        async with get_session() as session:
            fila = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider=provider, type_id=type_id,
                category=CATEGORY_COMMUNICATION, label=tipo, active=True, is_primary=False,
                external_id=external_id, credentials_encrypted=creds,
            )
            session.add(fila)
            await session.flush()
            return fila.id, external_id


async def _activa(tid: uuid.UUID, cid: uuid.UUID, activa: bool) -> None:
    """Lo mismo que hace `POST /admin/connections/{id}/action` (deactivate / activate)."""
    from core.gateway.connections import set_active
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await set_active(cid, activa)


async def _entrar(tid: uuid.UUID, cliente: str, external_id: str, provider: str) -> uuid.UUID:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(
            {
                "tenant_id": str(tid), "provider": provider, "channel": "whatsapp",
                "external_message_id": uniq("wamid"), "customer_external_id": cliente,
                "customer_name": "Cliente", "content": "hola, ¿están?", "connection_external_id": external_id,
                "media": "[]", "media_context": "",
            }
        )
    filas = await _filas(
        tid,
        "SELECT cv.id FROM conversations cv JOIN customers c ON c.id = cv.customer_id "
        "WHERE c.external_id = :e ORDER BY cv.started_at DESC LIMIT 1",
        e=cliente,
    )
    return filas[0]["id"]


async def _mensajes(tid: uuid.UUID, conv: uuid.UUID) -> list[tuple[str, str, str | None]]:
    filas = await _filas(
        tid,
        "SELECT direction, status, error_reason FROM messages WHERE conversation_id = :c ORDER BY created_at",
        c=conv,
    )
    return [(f["direction"], f["status"], f["error_reason"]) for f in filas]


def _pushes(monkeypatch) -> list[tuple[str, dict]]:
    import core.tasks.push_tasks as push_tasks

    pushes: list[tuple[str, dict]] = []
    monkeypatch.setattr(push_tasks.notify_owners_push, "delay", lambda _t, titulo, _c, datos: pushes.append((titulo, datos)))
    return pushes


def _sin_cola(monkeypatch) -> list[str]:
    import core.tasks.outbound_send as outbound_mod

    encolados: list[str] = []
    monkeypatch.setattr(outbound_mod.outbound_send_task, "delay", lambda *a, **_k: encolados.append(str(a[0])))
    return encolados


# ---------------------------------------------------------------- entrada
@pytest.mark.parametrize("tipo", ["manual", "waweb"])
async def test_lo_que_entra_a_una_conexion_pausada_se_guarda_sin_llm(monkeypatch, tipo: str) -> None:
    from core.gateway.meta_connection_state import TITULO_CONEXION_PROPIA_PAUSADA

    tid = await _negocio()
    cid, external_id = await _conexion(tid, tipo)
    await _activa(tid, cid, False)
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)
    encolados = _sin_cola(monkeypatch)
    pushes = _pushes(monkeypatch)
    proveedor = "meta" if tipo == "manual" else "waweb"

    cliente = uniq(f"wa-{tipo}")
    conv = await _entrar(tid, cliente, external_id, proveedor)
    await _entrar(tid, cliente, external_id, proveedor)

    assert await _turnos(tid, conv) == [("blocked", "connection_paused", "silent")] * 2
    assert [d for d, _s, _r in await _mensajes(tid, conv)] == ["inbound", "inbound"], (
        "se guarda para que el dueño lo vea al reanudar, y no se responde nada"
    )
    assert encolados == []
    assert pushes == [
        (TITULO_CONEXION_PROPIA_PAUSADA, {"type": "core.connection_paused", "connectionId": str(cid), "scope": "connection"})
    ], "un solo aviso por conexión y día"


# ---------------------------------------------------------------- salida
async def test_un_seguimiento_del_pack_no_sale_por_la_conexion_pausada(monkeypatch) -> None:
    from pack_commerce.jobs import _follow_up_leads_del_negocio
    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.gateway.meta_connection_state import MOTIVO_CONEXION_PAUSADA
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cid, external_id = await _conexion(tid, "waweb")
    _inmediato(monkeypatch)
    encolados = _sin_cola(monkeypatch)
    conv = await _entrar(tid, uniq("wa-seg"), external_id, "waweb")  # turno normal, conexión activa
    salientes_antes = len([m for m in await _mensajes(tid, conv) if m[0] == "outbound"])
    encolados.clear()

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                Lead(
                    tenant_id=tid, conversation_id=conv, name="Ana", interest="tortas", status="calificado",
                    last_activity_at=datetime.now(UTC) - timedelta(hours=2),
                )
            )
    await _activa(tid, cid, False)

    config = SimpleNamespace(follow_up_hours=1, follow_up_lead_message="Hola{name}, ¿seguimos con {interest}?")
    async with tenant_scope(tid):
        candidatos, _enviados = await _follow_up_leads_del_negocio(tid, config)

    assert candidatos == 1
    assert encolados == [], "no se encola nada para la conexión pausada"
    salientes = [m for m in await _mensajes(tid, conv) if m[0] == "outbound"][salientes_antes:]
    assert salientes == [("outbound", "failed", MOTIVO_CONEXION_PAUSADA)]


async def test_lo_encolado_antes_de_pausar_no_sale(monkeypatch) -> None:
    from test_conexion_canonica import _entrada

    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks import outbound_send

    tid = await _negocio()
    cid, external_id = await _conexion(tid, "manual")
    conv = (await _entrada(tid, uniq("wa-cola"), external_id)).id  # sin turno: nada más en cola
    async with tenant_scope(tid):
        async with get_session() as session:
            mensaje = Message(
                tenant_id=tid, conversation_id=conv, direction="outbound", sender_type="agent",
                content="respuesta en vuelo", status="queued",
            )
            session.add(mensaje)
            await session.flush()
            mid = mensaje.id
    await _activa(tid, cid, False)

    error = await outbound_send._send_one(mid, tid)
    assert isinstance(error, outbound_send.ConexionPausadaError)
    assert outbound_send.decide_retry(error, attempts_made=0).action == "fail_permanent"


# ---------------------------------------------------------------- reanudar y aislamiento
async def test_al_reanudar_vuelve_a_responder_y_otro_negocio_no_se_entera(monkeypatch) -> None:
    tid_a, tid_b = await _negocio(), await _negocio()
    cid_a, ext_a = await _conexion(tid_a, "waweb")
    _cid_b, ext_b = await _conexion(tid_b, "waweb")
    _inmediato(monkeypatch)
    _sin_cola(monkeypatch)
    _pushes(monkeypatch)
    await _activa(tid_a, cid_a, False)

    conv_b = await _entrar(tid_b, uniq("wa-b"), ext_b, "waweb")
    (turno_b,) = await _turnos(tid_b, conv_b)
    assert turno_b[1] is None, "la conexión del otro negocio sigue atendiendo"

    cliente_a = uniq("wa-a")
    conv_a = await _entrar(tid_a, cliente_a, ext_a, "waweb")
    assert await _turnos(tid_a, conv_a) == [("blocked", "connection_paused", "silent")]

    await _activa(tid_a, cid_a, True)
    await _entrar(tid_a, cliente_a, ext_a, "waweb")
    turnos = await _turnos(tid_a, conv_a)
    assert len(turnos) == 2 and turnos[1][1] is None, "reanudada, el turno vuelve a correr"
    assert any(d == "outbound" and s != "failed" for d, s, _r in await _mensajes(tid_a, conv_a))


# ---------------------------------------------------------------- filas de Meta
async def test_la_fila_canonica_de_meta_no_tiene_pausa_propia() -> None:
    from sqlalchemy import update
    from test_api_registrar_numero import _negocio_con_byok
    from test_conexion_canonica import _alta_numero, _conexiones, _entrada

    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.gateway.meta_connection_state import pausa_de_conversacion
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, _viejo = await _negocio_con_byok()
    numero = uniq("ph-canon")
    await _alta_numero(tid, cuenta, numero)
    (fila,) = await _conexiones(tid, numero)
    conv = await _entrada(tid, uniq("wa-canon"), numero)
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(update(ChannelConnection).where(ChannelConnection.id == fila.id).values(active=False))
        assert await pausa_de_conversacion(conv) is None, "manda la cuenta de Meta (ADR-0115 §2)"


async def test_choque_manual_inactiva_con_cuenta_viva_queda_en_silencio(monkeypatch) -> None:
    """Documentado en la adenda: la manual vieja del mismo número gana la resolución
    (`choque_manual`); desactivada, la conversación queda pausada aunque la cuenta esté viva."""
    from test_api_registrar_numero import _negocio_con_byok
    from test_conexion_canonica import _entrada

    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.gateway.connection_types import CATEGORY_COMMUNICATION, WHATSAPP_META
    from core.gateway.meta_connection_state import MOTIVO_CONEXION_PAUSADA, pausa_de_conversacion
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope

    tid, _cuenta, _waba, numero = await _negocio_con_byok()
    async with tenant_scope(tid):
        async with get_session() as session:
            manual = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="meta", type_id=WHATSAPP_META,
                category=CATEGORY_COMMUNICATION, label="manual vieja", active=False, is_primary=False,
                external_id=numero,
                credentials_encrypted=get_secret_box().encrypt_json({"phone_number_id": numero, "access_token": "t"}),
            )
            session.add(manual)
            await session.flush()
            mid = manual.id
    conv = await _entrada(tid, uniq("wa-choque"), numero)
    assert conv.connection_id == mid
    async with tenant_scope(tid):
        pausa = await pausa_de_conversacion(conv)
    assert pausa is not None and (pausa.motivo, pausa.alcance, pausa.id) == (MOTIVO_CONEXION_PAUSADA, "connection", mid)
