"""ADR-0205 §9: el aviso de lead nuevo sale con el Coordinador en foco o con un agente que tiene Captar leads; la fila
base se crea igual sin aviso (Cobranza, Agenda sin la habilidad). La creación se prueba en integración."""

from __future__ import annotations

from types import SimpleNamespace

from core.leads.asegurar import avisa_lead_nuevo


def _agente(role: str, tools: set[str]) -> SimpleNamespace:
    return SimpleNamespace(role=role, tools=frozenset(tools), declarativas=frozenset())


def test_avisa_con_el_coordinador_en_foco() -> None:
    assert avisa_lead_nuevo(_agente("core", set()))


def test_avisa_con_captar_leads_y_no_sin_ella() -> None:
    assert avisa_lead_nuevo(_agente("specialist", {"crear_lead", "calificar_lead"}))
    assert not avisa_lead_nuevo(_agente("specialist", {"cob_consultar_saldo"}))


def test_sin_foco_no_avisa() -> None:
    assert not avisa_lead_nuevo(None)
