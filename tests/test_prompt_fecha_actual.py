"""HU-CAL-04 / ADR-0091: la fecha y hora actual del negocio llega al prompt.

El dato va como ÚLTIMA parte del bloque dinámico (no invalida el cache de los
bloques estables) y la instrucción de cómo usarlo vive en el playbook
`reglas-operacion` (invariante 3: cero texto de instrucción en `.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agentcore_contracts import AgentSpec

from core.agents.responses import load_response
from core.llm.prompt_builder import PromptContext, build_system_blocks

# 2026-10-06 15:00 UTC = martes 10:00 en Bogotá (UTC-5).
MARTES_10_BOGOTA = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)

# Hoy martes 6: el "próximo miércoles" es el 7 y va primero en la lista.
PROXIMOS_DIAS = (
    "Próximos días: mañana miércoles 7 de octubre · jueves 8 de octubre · viernes 9 de octubre · sábado 10 de octubre · domingo 11 de octubre · lunes 12 de octubre · martes 13 de octubre."
)

PLAYBOOKS = [
    {"name": "identidad", "description": "Identidad del asistente"},
    {"name": "tono", "description": "Voz y tono del asistente"},
    {"name": "reglas-operacion", "description": "Reglas de operación del asistente"},
    {"name": "respuestas", "description": "Mensajes de cierre y avisos del asistente"},
]


def _spec() -> AgentSpec:
    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={
            "role": "asistente",
            "tone": "cercano",
            "warmth": 3,
            "emojis": "sparse",
            "maxLength": 500,
            "forbiddenPhrases": [],
        },
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=PLAYBOOKS,
        tools=[],
        policies={"maxToolCallsPerTurn": 10},
        packs=[],
    )


def _ctx(**overrides) -> PromptContext:
    base = {
        "assistant_name": "Asistente",
        "business_description": "Negocio de prueba",
        "timezone": "America/Bogota",
        "now": MARTES_10_BOGOTA,
    }
    base.update(overrides)
    return PromptContext(**base)


async def test_la_fecha_actual_es_la_ultima_parte_del_prompt() -> None:
    blocks = await build_system_blocks(_spec(), _ctx())

    assert blocks[-1] == (
        "## Fecha y hora actual\nHoy es martes, 6 de octubre de 2026 — 10:00 (America/Bogota).\n"
        "Próximos días: mañana miércoles 7 de octubre · jueves 8 de octubre · viernes 9 de octubre · sábado 10 de octubre · domingo 11 de octubre · lunes 12 de octubre · martes 13 de octubre."
    )


async def test_la_fecha_va_despues_del_resto_del_contexto_dinamico() -> None:
    blocks = await build_system_blocks(
        _spec(), _ctx(entity_profile="Nombre: Rob", recalled_memories=["Pidió una demo"])
    )

    assert "## Lo que sabes de este cliente" in blocks[-1]
    assert blocks[-1].endswith(
        "## Fecha y hora actual\nHoy es martes, 6 de octubre de 2026 — 10:00 (America/Bogota).\n"
        + PROXIMOS_DIAS
    )


async def test_la_hora_no_cambia_los_bloques_estables() -> None:
    """Dos turnos a distinta hora: solo cambia el último bloque (cache intacto)."""
    a = await build_system_blocks(_spec(), _ctx())
    b = await build_system_blocks(_spec(), _ctx(now=datetime(2026, 10, 6, 21, 37, tzinfo=UTC)))

    assert a[:-1] == b[:-1]
    assert a[-1] != b[-1]
    assert "16:37 (America/Bogota)." in b[-1]


async def test_usa_la_zona_horaria_del_negocio() -> None:
    blocks = await build_system_blocks(_spec(), _ctx(timezone="Asia/Tokyo"))

    # 15:00 UTC = 00:00 del miércoles en Tokio.
    assert "Hoy es miércoles, 7 de octubre de 2026 — 00:00 (Asia/Tokyo)." in blocks[-1]
    assert "Próximos días: mañana jueves 8 de octubre ·" in blocks[-1]


async def test_zona_invalida_cae_a_bogota() -> None:
    blocks = await build_system_blocks(_spec(), _ctx(timezone="Marte/Olimpo"))

    assert "Hoy es martes, 6 de octubre de 2026 — 10:00 (America/Bogota)." in blocks[-1]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Today is Tuesday, October 6, 2026 — 10:00 (America/Bogota)."),
        ("pt", "Hoje é terça-feira, 6 de outubro de 2026 — 10:00 (America/Bogota)."),
        ("fr", "Hoy es martes, 6 de octubre de 2026 — 10:00 (America/Bogota)."),
    ],
)
async def test_dia_y_mes_segun_el_idioma(language: str, expected: str) -> None:
    blocks = await build_system_blocks(_spec(), _ctx(language=language))

    assert expected in blocks[-1]


async def test_reglas_de_operacion_explican_la_fecha_y_prohiben_filtrar_instrucciones() -> None:
    """La instrucción vive en el playbook, no en el código (invariante 3)."""
    blocks = await build_system_blocks(_spec(), _ctx())
    reglas = next(b for b in blocks if "## Reglas de operación" in b)

    assert "Fecha y hora actual" in reglas
    assert "fechas u horas pasadas" in reglas
    assert "[SISTEMA]" in reglas


async def test_respuesta_verifying_coincide_con_la_constante() -> None:
    """ADR-0092: la respuesta neutral vive en `respuestas.md`; la constante es
    el respaldo determinista y debe ser idéntica."""
    from core.orchestrator.escalation import VERIFYING_REPLY

    assert await load_response(_spec(), "verifying") == VERIFYING_REPLY


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Next days: tomorrow Wednesday, October 7 · Thursday, October 8 ·"),
        ("pt", "Próximos dias: amanhã quarta-feira, 7 de outubro · quinta-feira, 8 de outubro ·"),
    ],
)
async def test_proximos_dias_segun_el_idioma(language: str, expected: str) -> None:
    """E2E HU-CAL-06: "el próximo miércoles" se resolvió al 14 en vez del 7. La
    lista de los próximos días con su fecha evita que el modelo haga cuentas."""
    blocks = await build_system_blocks(_spec(), _ctx(language=language))

    assert expected in blocks[-1]


async def test_reglas_remiten_a_la_lista_de_proximos_dias() -> None:
    blocks = await build_system_blocks(_spec(), _ctx())
    reglas = next(b for b in blocks if "## Reglas de operación" in b)

    assert "Próximos días" in reglas
