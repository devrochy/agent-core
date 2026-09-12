"""`evals/runner.py::_build_messages` -- el historial de un escenario debe
poder cargar tool_calls declarados (mismo shape que produce un turno real,
`core/orchestrator/loop.py`), no solo texto plano.

Hallazgo (ver `docs/ROADMAP.md`): `venta-recomendar-producto-03` y
`seguimiento-calificar-lead-22` seguían flaky porque el historial falso
del escenario nunca le daba al modelo evidencia estructurada de que una
tool ya había corrido en un turno anterior -- solo una afirmación en
texto plano. Este test cubre el helper que lo arregla, sin BD ni LLM.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `evals/` no es un paquete instalado -- se agrega al sys.path como hace
# el propio runner.py al ejecutarse como script.
EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from runner import _build_messages  # noqa: E402


def test_build_messages_sin_tool_calls_es_igual_que_antes():
    """Regresión: los escenarios que NO declaran tool_calls (la mayoría del
    golden set) deben producir exactamente el LLMMessage(role, content) de
    siempre -- cero cambio de comportamiento."""
    conversation = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¡Hola! ¿En qué te ayudo?"},
    ]

    messages = _build_messages(conversation)

    assert len(messages) == 2
    assert [m.role for m in messages] == ["user", "assistant"]
    assert [m.content for m in messages] == ["hola", "¡Hola! ¿En qué te ayudo?"]
    assert all(m.tool_calls == [] for m in messages)
    assert all(m.tool_call_id is None for m in messages)


def test_build_messages_con_tool_calls_arma_assistant_mas_tool_result():
    """Un turno con tool_calls declarados debe producir el par
    assistant(tool_calls=[...]) + tool(tool_call_id=..., content=...),
    con el mismo id enlazando ambos -- la forma real que espera cualquier
    proveedor LLM para tool-calling multi-turno."""
    conversation = [
        {"role": "user", "content": "quiero X"},
        {
            "role": "assistant",
            "content": "tenemos X",
            "tool_calls": [
                {"name": "consultar_inventario", "arguments": {"query": "X"}, "result": {"matches": [{"sku": "X-1"}]}}
            ],
        },
        {"role": "user", "content": "recomendame algo"},
    ]

    messages = _build_messages(conversation)

    assert len(messages) == 4
    user1, assistant, tool_result, user2 = messages

    assert user1.role == "user"
    assert user1.content == "quiero X"

    assert assistant.role == "assistant"
    assert assistant.content == "tenemos X"
    assert len(assistant.tool_calls) == 1
    assert assistant.tool_calls[0].name == "consultar_inventario"
    assert assistant.tool_calls[0].arguments == {"query": "X"}

    assert tool_result.role == "tool"
    assert tool_result.tool_call_id == assistant.tool_calls[0].id
    assert '"sku": "X-1"' in tool_result.content

    assert user2.role == "user"
    assert user2.content == "recomendame algo"


def test_build_messages_multiples_tool_calls_en_un_turno_tienen_ids_distintos():
    conversation = [
        {
            "role": "assistant",
            "content": "listo",
            "tool_calls": [
                {"name": "tool_a", "arguments": {}, "result": {"a": 1}},
                {"name": "tool_b", "arguments": {}, "result": {"b": 2}},
            ],
        },
    ]

    messages = _build_messages(conversation)

    assert len(messages) == 3
    assistant, tool_a, tool_b = messages
    ids = {tc.id for tc in assistant.tool_calls}
    assert len(ids) == 2, "cada tool_call declarada debe tener su propio id"
    assert tool_a.tool_call_id in ids
    assert tool_b.tool_call_id in ids
    assert tool_a.tool_call_id != tool_b.tool_call_id
