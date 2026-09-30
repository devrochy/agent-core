"""Tests puros del modo prueba del panel (playground, Fase CT).

Sin base de datos: se verifica el contrato del guard de escalamiento y la
constante de canal, que es lo que garantiza que una prueba del panel no
contamine la cola de escalaciones del tenant.
"""

from __future__ import annotations

import types
import uuid
from typing import Any

import pytest

from core.orchestrator import escalation
from core.shared.test_mode import TEST_CHANNEL, is_test_channel


def test_test_channel_no_es_whatsapp() -> None:
    assert TEST_CHANNEL == "test"
    assert is_test_channel("test") is True
    assert is_test_channel("whatsapp") is False
    assert is_test_channel(None) is False


class _FakeSession:
    def __init__(self, conversation: Any) -> None:
        self._conversation = conversation
        self.added: list[Any] = []

    async def get(self, model: Any, id_: Any) -> Any:
        return self._conversation

    def add(self, obj: Any) -> None:
        self.added.append(obj)


class _FakeCM:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeBus:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: str, **kwargs: Any) -> None:
        self.emitted.append((event, kwargs))


@pytest.mark.asyncio
async def test_escalate_ignora_conversacion_de_prueba(monkeypatch: pytest.MonkeyPatch) -> None:
    """Una conversación `channel == "test"` NO crea Escalation ni pausa el bot."""
    conversation = types.SimpleNamespace(channel=TEST_CHANNEL, status="open", bot_paused=False)
    session = _FakeSession(conversation)
    monkeypatch.setattr(escalation, "get_session", lambda: _FakeCM(session))

    bus = _FakeBus()
    monkeypatch.setattr(escalation, "event_bus", bus)

    async def _noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(escalation, "record_audit", _noop_audit)

    await escalation.escalate(uuid.uuid4(), reason="guardrail", summary="prueba")

    assert session.added == [], "el playground no debe crear Escalation"
    assert conversation.status == "open"
    assert conversation.bot_paused is False
    assert bus.emitted == []


@pytest.mark.asyncio
async def test_escalate_sigue_funcionando_en_canal_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: una conversación real sí escala (el guard no rompe el flujo)."""
    conversation = types.SimpleNamespace(channel="whatsapp", status="open", bot_paused=False)
    session = _FakeSession(conversation)
    monkeypatch.setattr(escalation, "get_session", lambda: _FakeCM(session))

    bus = _FakeBus()
    monkeypatch.setattr(escalation, "event_bus", bus)

    async def _noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(escalation, "record_audit", _noop_audit)

    await escalation.escalate(uuid.uuid4(), reason="guardrail", summary="real")

    assert conversation.status == "escalated"
    assert conversation.bot_paused is True
    assert len(session.added) == 1
    assert len(bus.emitted) == 1
