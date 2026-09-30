"""pack-calendar declara su flujo de atención (ADR-0054).

Sin `flow.json`, al haber otro pack con flujo (p. ej. pack_commerce) el
`resolve_flow` resuelve un flujo que NO incluye las tools de calendario, así que
el asistente nunca las recibe y no agenda.
"""

from __future__ import annotations

from core.agents.flow import load_pack_flows


def test_pack_calendar_declara_flujo_de_agendamiento():
    sections = load_pack_flows(["pack_calendar"])
    assert sections, "pack_calendar debe declarar flow.json"
    tools = {t for section in sections for t in section.tools}
    assert "check_availability" in tools
    assert "create_calendar_event" in tools
