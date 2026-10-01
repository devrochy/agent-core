"""pack-subscriptions declara su flujo de atención (ADR-0054, planes como productos)."""

from __future__ import annotations

from core.agents.flow import load_pack_flows


def test_pack_subscriptions_declara_flujo_de_gestion():
    sections = load_pack_flows(["pack_subscriptions"])
    assert sections, "pack_subscriptions debe declarar flow.json"
    tools = {t for section in sections for t in section.tools}
    assert "sub_consultar_suscripcion" in tools
    assert "sub_pausar_suscripcion" in tools
    assert "sub_link_pago" in tools
    # Los planes los informa commerce: este pack no expone tools de planes.
    assert "sub_informar_planes" not in tools
    assert "sub_crear_suscripcion" not in tools


def test_secciones_con_ids_unicos():
    sections = load_pack_flows(["pack_subscriptions"])
    ids = [s.id for s in sections]
    assert len(ids) == len(set(ids)), "los ids de sección deben ser únicos (ADR-0054)"
    assert all(i.startswith("sub_") for i in ids), "namespacing sub_* (ADR-0083)"