"""HU-AOS-75: el flujo del agente en la resolución y en el manifiesto de la release.

- `resolve_flow(flow_agente=...)` compone core + el flujo del agente, sin los defaults de sus
  packs (el agente ya los tiene copiados).
- El manifiesto congela el flujo de cada componente y su hash; cambiarlo cambia el hash de la
  release.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.agents.flow import resolve_flow
from core.releases import manifiesto as man


@pytest.mark.asyncio
async def test_resolve_flow_agente_no_mezcla_los_defaults_del_pack() -> None:
    spec = SimpleNamespace(packs=["pack_commerce"], tenantId="t")
    flujo = await resolve_flow(
        spec,
        flow_agente={
            "sections": [
                {"id": "propia", "label": "Propia", "purpose": "Solo este agente", "tools": ["consultar_inventario"]}
            ]
        },
        available={"consultar_inventario"},
    )
    assert flujo is not None
    assert flujo.source == "agente"
    ids = [s.id for s in flujo.sections]
    assert "propia" in ids
    assert "descubrimiento" not in ids  # no se compone el flow.json del pack
    assert flujo.tool_names == ["consultar_inventario"]


@pytest.mark.asyncio
async def test_sin_flow_agente_sigue_componiendo_el_pack() -> None:
    spec = SimpleNamespace(packs=["pack_commerce"], tenantId="t")
    flujo = await resolve_flow(
        spec,
        flow_tenant=None,
        available={"consultar_inventario", "generar_cotizacion", "crear_pedido"},
    )
    assert flujo is not None
    assert flujo.source == "pack"
    assert "descubrimiento" in [s.id for s in flujo.sections]


def _componente(flow: dict, flow_hash: str) -> man.Componente:
    return man.Componente(
        instance_id="i-1",
        agent_id="a-1",
        agent_key="ventas",
        role="specialist",
        agent_version_id="v-1",
        version=1,
        content_hash="contenido",
        contract="agentspec/3",
        flow=flow,
        flow_hash=flow_hash,
    )


def _manifiesto(componente: man.Componente) -> dict:
    return man.armar(
        componentes=[componente],
        habilidades=[],
        plugins_del_negocio=[],
        overrides={},
        flujo_del_negocio=None,
        app_settings={},
    )


def test_manifiesto_congela_el_flujo_por_agente_y_su_hash() -> None:
    a = _componente({"sections": [{"id": "a", "label": "A", "purpose": "", "tools": ["x"]}]}, "hash-a")
    b = _componente({"sections": [{"id": "a", "label": "A", "purpose": "", "tools": ["y"]}]}, "hash-b")
    ma = _manifiesto(a)
    mb = _manifiesto(b)
    assert ma["components"][0]["flow"]["sha256"] == "hash-a"
    assert ma["components"][0]["flow"]["sections"] == a.flow
    assert man.hash_de(ma) != man.hash_de(mb)
