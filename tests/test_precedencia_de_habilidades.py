"""HU-AOS-63, Gherkin «Precedencia»: con una release `release/2` manda conexión › agente › negocio (ADR-0118
enmendado, ADR-0190 §5)."""

from __future__ import annotations

from types import SimpleNamespace

from core.kernel.procedures.ejecucion import packs_del_turno


def _release(skills: set[str], plugins: frozenset[str] = frozenset()) -> SimpleNamespace:
    return SimpleNamespace(skills=sorted(skills), tenant_plugins=sorted(plugins))


def test_la_conexion_acota_lo_que_el_agente_tiene_instalado() -> None:
    # El negocio tiene Comercio, Agenda y Cobranza; la release de la conexión congeló Comercio y Agenda; el agente tiene
    # instaladas Agenda y Cobranza. Solo queda Agenda: Cobranza no está en la conexión y Comercio no es del agente.
    release = _release({"pack_commerce", "pack_calendar"}, {"plugin_del_negocio"})
    agente = SimpleNamespace(packs=frozenset({"pack_calendar", "pack_cobranza"}))
    assert packs_del_turno(release, agente) == {"pack_calendar", "plugin_del_negocio"}


def test_el_agente_sin_habilidades_de_la_conexion_queda_solo_con_los_plugins() -> None:
    release = _release({"pack_commerce"}, {"plugin_del_negocio"})
    agente = SimpleNamespace(packs=frozenset({"pack_cobranza"}))
    assert packs_del_turno(release, agente) == {"plugin_del_negocio"}


def test_sin_agente_en_foco_o_sin_instalaciones_congeladas_rige_la_release() -> None:
    release = _release({"pack_commerce", "pack_calendar"})
    assert packs_del_turno(release, None) == {"pack_commerce", "pack_calendar"}
    assert packs_del_turno(release, SimpleNamespace(packs=None)) == {"pack_commerce", "pack_calendar"}
