"""Iconos veraces de la columna Agentes (HU-CRM-12, ADR-0179): `con_datos` manda sobre la heurística."""

from __future__ import annotations

import uuid

from core.admin.lead_registry import LeadColumn, LeadProvider, LeadRelacion
from core.api.routers.leads import LeadAgentOut, _agentes_de_la_fila

CLIENTE = uuid.uuid4()
OTRO = uuid.uuid4()


async def _nada(_ids):  # pragma: no cover - solo se declara; la fila no la ejecuta
    raise AssertionError


META = [
    LeadAgentOut(key="general", name="General", icon="bot", columns=[]),
    LeadAgentOut(key="ventas", name="Ventas", icon="cart", columns=[]),
    LeadAgentOut(key="otro", name="Otro", icon="bot", columns=[]),
]
# El caso real de commerce: el resolver devuelve «Nuevo» y 0 pedidos a todo cliente.
EXTRAS_VACIOS = {"clasificacion": "Nuevo", "pedidos": 0}


def _ventas(con_relacion: bool) -> LeadProvider:
    return LeadProvider(
        "pack_commerce",
        columns=[LeadColumn("clasificacion", "Clasificación"), LeadColumn("pedidos", "Pedidos")],
        agent_key="ventas",
        relacion=LeadRelacion(con_datos=_nada) if con_relacion else None,
    )


def test_cliente_sin_pedidos_solo_ve_general() -> None:
    claves = _agentes_de_la_fila(
        [_ventas(True)], EXTRAS_VACIOS, META, CLIENTE, {"pack_commerce": {OTRO}}
    )
    assert claves == ["general"]


def test_cliente_con_pedido_ve_ventas_y_el_otro_no() -> None:
    con_datos = {"pack_commerce": {CLIENTE}}
    assert _agentes_de_la_fila([_ventas(True)], EXTRAS_VACIOS, META, CLIENTE, con_datos) == ["general", "ventas"]
    assert _agentes_de_la_fila([_ventas(True)], EXTRAS_VACIOS, META, OTRO, con_datos) == ["general"]


def test_provider_sin_relacion_conserva_la_heuristica() -> None:
    assert _agentes_de_la_fila([_ventas(False)], {"pedidos": 3}, META, CLIENTE, {}) == ["general", "ventas"]
    assert _agentes_de_la_fila([_ventas(False)], {"pedidos": None}, META, CLIENTE, {}) == ["general"]
