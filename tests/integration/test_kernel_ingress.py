"""Políticas de entrada del kernel contra el stack real (HU-AOS-10, parte 2).

Las políticas del consumer viven en `core.kernel.ingress.politicas_de_entrada` con el mismo
orden y las mismas respuestas de antes. Cada una deja su decisión en `core_decisions` y su
resultado en `turn_outcomes`, sin llamar al LLM:

- palabra de escalamiento → `escalated/escalation_keyword`, escala y responde el texto fijo;
- fuera de horario → `blocked/out_of_hours`, `replied` con el mensaje configurado o `silent`;
- conversación escalada (y por eso pausada) → `escalated_conversation`, no `kill_switch`;
- conexión pausada y negocio pausado a la vez → gana la conexión (va primero) y avisa;
- turno diferido con la conexión pausada → `connection_paused` sin avisar otra vez.

El LLM es el `FakeProvider` del conftest o uno que falla si lo llaman: nunca hay LLM real.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import select, text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # camino del consumer y del debounce con RLS de las tablas del kernel
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# Una franja vacía (de 00:00 a 00:00): el negocio nunca está abierto.
SIEMPRE_CERRADO = {"tz": "UTC", "ranges": [{"day": d, "from": "00:00", "to": "00:00"} for d in range(7)]}


def _inbound(tid: uuid.UUID, cliente: str, contenido: str, phone: str) -> dict:
    return {
        "tenant_id": str(tid),
        "provider": "meta",
        "channel": "whatsapp",
        "external_message_id": uniq("wamid"),
        "customer_external_id": cliente,
        "customer_name": "Cliente",
        "content": contenido,
        "connection_external_id": phone,
        "media": "[]",
        "media_context": "",
    }


def _inmediato(monkeypatch) -> None:
    from core.config import DEBOUNCE_DELAY_MS

    for modo in list(DEBOUNCE_DELAY_MS):
        monkeypatch.setitem(DEBOUNCE_DELAY_MS, modo, 0)


def _sin_llm(monkeypatch) -> None:
    class _Prohibido:
        provider_name = "prohibido"

        def estimate_cost_usd(self, *_a: Any) -> float:
            return 0.0

        async def generate(self, **_kwargs: Any):
            raise AssertionError("se llamó al LLM en un turno bloqueado")

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: _Prohibido())


def _avisos(monkeypatch) -> list[str]:
    import core.tasks.push_tasks as push_tasks

    pushes: list[str] = []
    monkeypatch.setattr(
        push_tasks.notify_owners_push, "delay", lambda _tid, titulo, *_a, **_k: pushes.append(titulo)
    )
    return pushes


async def _ajustes(tid: uuid.UUID, **valores: Any) -> None:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            ajustes = (
                await session.execute(
                    select(AppSettings).where(
                        AppSettings.tenant_id == tid, AppSettings.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if ajustes is None:
                ajustes = AppSettings(tenant_id=tid)
                session.add(ajustes)
            for clave, valor in valores.items():
                setattr(ajustes, clave, valor)


async def _negocio(**ajustes: Any) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("kin")
    tid, _ = await onboard_tenant(
        name=f"K {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local"
    )
    if ajustes:
        await _ajustes(tid, **ajustes)
    return tid


async def _filas(tid: uuid.UUID, sql: str, **params: Any) -> list[dict[str, Any]]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return [dict(r._mapping) for r in (await session.execute(text(sql), params)).all()]


async def _entrar(tid: uuid.UUID, cliente: str, contenido: str, phone: str | None = None) -> uuid.UUID:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(
            _inbound(tid, cliente, contenido, phone or uniq("sin-conexion"))
        )
    filas = await _filas(
        tid,
        "SELECT cv.id FROM conversations cv JOIN customers c ON c.id = cv.customer_id "
        "WHERE c.external_id = :e ORDER BY cv.started_at DESC LIMIT 1",
        e=cliente,
    )
    return filas[0]["id"]


async def _turnos(tid: uuid.UUID, conv: uuid.UUID) -> list[tuple[str, str | None, str | None]]:
    """(outcome, blocked_by, status) de cada turno de la conversación, en orden."""
    filas = await _filas(
        tid,
        "SELECT d.outcome, d.blocked_by, o.status FROM core_decisions d "
        "LEFT JOIN turn_outcomes o ON o.turn_id = d.id "
        "WHERE d.conversation_id = :c ORDER BY d.created_at",
        c=conv,
    )
    return [(f["outcome"], f["blocked_by"], f["status"]) for f in filas]


async def _salientes(tid: uuid.UUID, conv: uuid.UUID) -> list[str]:
    filas = await _filas(
        tid,
        "SELECT content FROM messages WHERE conversation_id = :c AND direction = 'outbound' "
        "ORDER BY created_at",
        c=conv,
    )
    return [f["content"] for f in filas]


async def _escalaciones(tid: uuid.UUID, conv: uuid.UUID) -> list[str]:
    filas = await _filas(
        tid, "SELECT reason FROM escalations WHERE conversation_id = :c ORDER BY created_at", c=conv
    )
    return [f["reason"] for f in filas]


# ---------------------------------------------------------------- palabras de escalamiento
async def test_palabra_de_escalamiento_escala_y_responde_sin_llm(monkeypatch) -> None:
    tid = await _negocio(escalation_config={"keywords": ["quiero un humano"]})
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    conv = await _entrar(tid, uniq("wa-kw"), "Hola, QUIERO UN HUMANO ya")

    assert await _turnos(tid, conv) == [("escalated", "escalation_keyword", "escalated")]
    assert await _escalaciones(tid, conv) == ["keyword"]
    assert len(await _salientes(tid, conv)) == 1, "la respuesta fija de escalamiento"


# ---------------------------------------------------------------- horario de atención
async def test_fuera_de_horario_responde_el_mensaje_configurado_sin_llm(monkeypatch) -> None:
    tid = await _negocio(
        assistant_name="Lupe",
        escalation_config={
            "business_hours": SIEMPRE_CERRADO,
            "out_of_hours_message": "  Soy {assistant_name}: ya cerramos, mañana te respondo.  ",
        },
    )
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    conv = await _entrar(tid, uniq("wa-hor"), "hola")

    assert await _turnos(tid, conv) == [("blocked", "out_of_hours", "replied")]
    assert await _salientes(tid, conv) == ["Soy Lupe: ya cerramos, mañana te respondo."]
    assert await _escalaciones(tid, conv) == []


async def test_fuera_de_horario_sin_mensaje_no_responde(monkeypatch) -> None:
    tid = await _negocio(escalation_config={"business_hours": SIEMPRE_CERRADO})
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    conv = await _entrar(tid, uniq("wa-hor0"), "hola")

    assert await _turnos(tid, conv) == [("blocked", "out_of_hours", "silent")]
    assert await _salientes(tid, conv) == []


async def test_fuera_de_horario_con_envio_fallido_escala(monkeypatch) -> None:
    tid = await _negocio(
        escalation_config={"business_hours": SIEMPRE_CERRADO, "out_of_hours_message": "Cerrado"}
    )
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    async def _no_sale(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr("core.gateway.sender.send_to_conversation", _no_sale)

    conv = await _entrar(tid, uniq("wa-horx"), "hola")

    assert await _turnos(tid, conv) == [("blocked", "out_of_hours", "escalated")]
    assert await _escalaciones(tid, conv) == ["send_failed"]


# ---------------------------------------------------------------- orden de las políticas
async def test_conversacion_escalada_gana_al_kill_switch(monkeypatch) -> None:
    from core.orchestrator import consumer
    from core.orchestrator.escalation import escalate
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente = uniq("wa-esc")
    async with tenant_scope(tid):
        conversacion, _c, _s = await consumer._resolve_conversation(
            {"channel": "whatsapp", "customer_external_id": cliente, "connection_external_id": "x"}
        )
        # `escalate` deja la conversación escalada Y con el bot pausado: el empate real.
        await escalate(conversacion.id, reason="test", summary="")
    pausada = await _filas(
        tid, "SELECT status, bot_paused FROM conversations WHERE id = :c", c=conversacion.id
    )
    assert pausada == [{"status": "escalated", "bot_paused": True}]
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    conv = await _entrar(tid, cliente, "quiero un humano")

    assert conv == conversacion.id
    assert await _turnos(tid, conv) == [("blocked", "escalated_conversation", "silent")]
    assert await _escalaciones(tid, conv) == ["test"], "una conversación escalada no escala otra vez"


async def test_conexion_pausada_va_antes_que_el_negocio_pausado(monkeypatch) -> None:
    from test_api_registrar_numero import _negocio_con_byok
    from test_conexion_ciclo_de_vida import _estado

    tid, cid, _waba, phone_id = await _negocio_con_byok()
    await _estado(tid, cid, "revoked")
    await _ajustes(tid, bot_paused=True)
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)
    avisos = _avisos(monkeypatch)

    conv = await _entrar(tid, uniq("wa-cpk"), "hola", phone=phone_id)

    assert await _turnos(tid, conv) == [("blocked", "connection_paused", "silent")]
    assert len(avisos) == 1, "se avisa a los dueños, como antes"


async def test_negocio_pausado_es_kill_switch(monkeypatch) -> None:
    tid = await _negocio(bot_paused=True, escalation_config={"keywords": ["humano"]})
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    conv = await _entrar(tid, uniq("wa-ks"), "quiero un humano")

    assert await _turnos(tid, conv) == [("blocked", "kill_switch", "silent")], (
        "el kill-switch va antes que las palabras"
    )
    assert await _salientes(tid, conv) == []


# ---------------------------------------------------------------- turno diferido
async def test_turno_diferido_con_la_conexion_pausada_no_avisa_otra_vez(monkeypatch) -> None:
    from test_api_registrar_numero import _negocio_con_byok
    from test_conexion_ciclo_de_vida import _estado

    from core.orchestrator import debounce

    tid, cid, _waba, phone_id = await _negocio_con_byok()
    _inmediato(monkeypatch)
    conv = await _entrar(tid, uniq("wa-dif"), "hola", phone=phone_id)  # turno normal
    await _estado(tid, cid, "revoked")
    _sin_llm(monkeypatch)
    avisos = _avisos(monkeypatch)

    await debounce._fire_conversation(conv, {"latest_message": "hola", "tenant_id": str(tid)})

    turnos = await _turnos(tid, conv)
    assert turnos[0][0] == "routed" and turnos[1:] == [("blocked", "connection_paused", "silent")]
    assert avisos == [], "al disparar no se avisa: ya se avisó al entrar el mensaje"


# ---------------------------------------------------------------- fallos al escalar o responder
async def _conversacion_de(tid: uuid.UUID, cliente: str) -> uuid.UUID:
    filas = await _filas(
        tid,
        "SELECT cv.id FROM conversations cv JOIN customers c ON c.id = cv.customer_id "
        "WHERE c.external_id = :e",
        e=cliente,
    )
    return filas[0]["id"]


async def test_palabra_de_escalamiento_escala_aunque_no_cargue_el_spec(monkeypatch) -> None:
    """Antes, si `load_active_spec` fallaba quedaba la decisión `escalated` sin escalamiento:
    el reintento del consumer ve el inbound como duplicado y no lo rehace. Ahora escala y
    responde el texto fijo."""
    from core.orchestrator.escalation import ESCALATION_REPLY

    tid = await _negocio(escalation_config={"keywords": ["quiero un humano"]})
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    async def _spec_caido(*_a: Any, **_k: Any) -> Any:
        raise ConnectionError("BD caída")

    monkeypatch.setattr("core.agents.spec.load_active_spec", _spec_caido)

    conv = await _entrar(tid, uniq("wa-kws"), "quiero un humano")

    assert await _turnos(tid, conv) == [("escalated", "escalation_keyword", "escalated")]
    assert await _escalaciones(tid, conv) == ["keyword"]
    assert await _salientes(tid, conv) == [ESCALATION_REPLY]


async def test_palabra_de_escalamiento_reintenta_si_escalar_falla_una_vez(monkeypatch) -> None:
    from core.orchestrator import escalation

    tid = await _negocio(escalation_config={"keywords": ["quiero un humano"]})
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)
    original = escalation.escalate
    intentos: list[str] = []

    async def _falla_una_vez(*a: Any, **k: Any) -> None:
        intentos.append(k.get("reason", ""))
        if len(intentos) == 1:
            raise ConnectionError("conexión cortada")
        await original(*a, **k)

    monkeypatch.setattr("core.orchestrator.escalation.escalate", _falla_una_vez)
    monkeypatch.setattr("core.kernel.ingress._ESPERA_REINTENTO_ESCALAR_S", 0)

    conv = await _entrar(tid, uniq("wa-kwr"), "quiero un humano")

    assert intentos == ["keyword", "keyword"]
    assert await _turnos(tid, conv) == [("escalated", "escalation_keyword", "escalated")]
    assert await _escalaciones(tid, conv) == ["keyword"]
    assert len(await _salientes(tid, conv)) == 1


async def test_palabra_de_escalamiento_sin_poder_escalar_falla_a_la_vista(monkeypatch) -> None:
    """Si escalar falla también al reintentar, el turno queda `failed` (decisión y resultado
    coherentes), el error sube al consumer, se avisa a los dueños (el reintento del consumer no
    rehace el turno) y no se le promete una persona al cliente."""
    tid = await _negocio(escalation_config={"keywords": ["quiero un humano"]})
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)
    avisos = _avisos(monkeypatch)
    monkeypatch.setattr("core.kernel.ingress._ESPERA_REINTENTO_ESCALAR_S", 0)

    async def _no_escala(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("BD caída")

    monkeypatch.setattr("core.orchestrator.escalation.escalate", _no_escala)
    cliente = uniq("wa-kwf")

    with pytest.raises(ConnectionError):
        await _entrar(tid, cliente, "quiero un humano")

    conv = await _conversacion_de(tid, cliente)
    assert await _turnos(tid, conv) == [("escalated", "escalation_keyword", "failed")]
    assert await _salientes(tid, conv) == []
    assert avisos == ["Nueva escalación"]


async def test_palabra_de_escalamiento_con_envio_que_lanza_queda_escalada(monkeypatch) -> None:
    """Si el aviso al cliente no sale después de escalar, el turno sigue `escalated`: la
    persona igual llega."""
    tid = await _negocio(escalation_config={"keywords": ["quiero un humano"]})
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    async def _revienta(*_a: Any, **_k: Any) -> bool:
        raise ConnectionError("broker caído")

    monkeypatch.setattr("core.gateway.sender.send_to_conversation", _revienta)

    conv = await _entrar(tid, uniq("wa-kwe"), "quiero un humano")

    assert await _turnos(tid, conv) == [("escalated", "escalation_keyword", "escalated")]
    assert await _escalaciones(tid, conv) == ["keyword"]
    assert await _filas(
        tid, "SELECT status, bot_paused FROM conversations WHERE id = :c", c=conv
    ) == [{"status": "escalated", "bot_paused": True}]


async def test_fuera_de_horario_con_envio_que_lanza_escala(monkeypatch) -> None:
    """Antes solo se escalaba si el envío devolvía False; si lanzaba, el cliente se quedaba
    sin respuesta y sin persona."""
    tid = await _negocio(
        escalation_config={"business_hours": SIEMPRE_CERRADO, "out_of_hours_message": "Cerrado"}
    )
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)

    async def _revienta(*_a: Any, **_k: Any) -> bool:
        raise ConnectionError("broker caído")

    monkeypatch.setattr("core.gateway.sender.send_to_conversation", _revienta)

    conv = await _entrar(tid, uniq("wa-horl"), "hola")

    assert await _turnos(tid, conv) == [("blocked", "out_of_hours", "escalated")]
    assert await _escalaciones(tid, conv) == ["send_failed"]
    assert await _filas(
        tid, "SELECT status, bot_paused FROM conversations WHERE id = :c", c=conv
    ) == [{"status": "escalated", "bot_paused": True}]
