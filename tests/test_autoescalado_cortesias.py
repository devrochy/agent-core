"""ADR-0104 (enmienda ADR-0051): el autoescalado por turnos sin resolver no
cuenta cortesías ni turnos en que el asistente pregunta para avanzar.

Antes, tres "Hola" seguidos (o un ciclo de preguntas pidiendo un dato) sumaban
`turnos_sin_resolver` y escalaban la conversación pausando el bot (issue #234)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import core.orchestrator.loop as loop
from core.orchestrator.loop import (
    _es_cortesia,
    _finish_turn,
    _pregunta_de_avance,
    _turno_avanza_sin_tools,
)


@pytest.mark.parametrize(
    "texto",
    [
        "hola",
        "Hola!",
        "Holaaaa ",
        "buenas tardes",
        "Buenos días!",
        "hey",
        "¿qué tal?",
        "gracias",
        "Muchas gracias",
        "ok",
        "Okay!",
        "listo",
        "perfecto",
        "de acuerdo",
        "entendido",
        "dale",
    ],
)
def test_cortesias_sueltas(texto: str) -> None:
    assert _es_cortesia(texto)


@pytest.mark.parametrize(
    "texto",
    [
        "hola, quiero una demo",
        "buenas, cuánto vale el plan",
        "gracias, pero necesito la fecha",
        "quiero agendar",
        "sí",
        "no",
        "",
    ],
)
def test_no_son_cortesias(texto: str) -> None:
    assert not _es_cortesia(texto)


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("¿Para qué día quieres la demo?", True),
        ("Te agendo para el miércoles.", False),
        ("¿Me confirmas tu correo?", True),
        ("Listo, quedó agendada.", False),
    ],
)
def test_pregunta_de_avance(texto: str, esperado: bool) -> None:
    assert _pregunta_de_avance(texto) is esperado


def test_turno_avanza_sin_tools() -> None:
    assert _turno_avanza_sin_tools("hola", "¡Hola! Cuéntame.", hubo_tools=False)
    assert _turno_avanza_sin_tools("quiero una demo", "¿Para cuándo la agendo?", hubo_tools=False)
    assert _turno_avanza_sin_tools("lo que sea", "Hice la reserva.", hubo_tools=True)
    assert not _turno_avanza_sin_tools("quiero una demo", "Ahora mismo reviso.", hubo_tools=False)


def _fake_session(conversation: SimpleNamespace):
    class FakeSession:
        async def get(self, model, id_):
            return conversation

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return FakeSession()


def _patch_memorias(monkeypatch) -> None:
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(loop.working_memory, "append_turn", _noop)
    monkeypatch.setattr(loop.vector_memory, "store_memory", _noop)


@pytest.mark.asyncio
async def test_cortesia_conserva_el_contador_sin_sumar_ni_reiniciar(monkeypatch) -> None:
    conversation = SimpleNamespace(state={"turnos_sin_resolver": 2})
    monkeypatch.setattr(loop, "get_session", lambda: _fake_session(conversation))
    _patch_memorias(monkeypatch)

    resultado = await _finish_turn(
        uuid.uuid4(), "¡Hola! ¿En qué te ayudo?", "hola", resolved=False, count_unresolved=False
    )

    assert resultado == 2
    assert conversation.state["turnos_sin_resolver"] == 2


@pytest.mark.asyncio
async def test_turno_sin_avance_si_suma(monkeypatch) -> None:
    conversation = SimpleNamespace(state={"turnos_sin_resolver": 2})
    monkeypatch.setattr(loop, "get_session", lambda: _fake_session(conversation))
    _patch_memorias(monkeypatch)

    resultado = await _finish_turn(
        uuid.uuid4(), "Ahora mismo reviso.", "¿me ayudas?", resolved=False, count_unresolved=True
    )

    assert resultado == 3
    assert conversation.state["turnos_sin_resolver"] == 3


@pytest.mark.asyncio
async def test_turno_con_tools_reinicia_el_contador(monkeypatch) -> None:
    conversation = SimpleNamespace(state={"turnos_sin_resolver": 4})
    monkeypatch.setattr(loop, "get_session", lambda: _fake_session(conversation))
    _patch_memorias(monkeypatch)

    resultado = await _finish_turn(
        uuid.uuid4(), "Listo.", "quiero una demo", resolved=True, count_unresolved=False
    )

    assert resultado == 0
    assert conversation.state["turnos_sin_resolver"] == 0
