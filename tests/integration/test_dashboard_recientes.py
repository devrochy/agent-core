"""`GET /admin/dashboard`: «Conversaciones recientes» del Resumen.

- **Contenido:** cada conversación trae cliente, estado, canal y cuándo empezó (para el avatar y el
  «hace X» del panel).
- **Aislamiento:** un negocio no ve las conversaciones de otro.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import api_headers
from test_agent_data_api import _conexion, _conversacion
from test_byok_produccion import _negocio

pytestmark = [
    pytest.mark.aislamiento,  # recientes entre negocios (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_URL = "/api/v1/admin/dashboard"


async def test_recientes_traen_canal_y_cuando_empezo(app_client) -> None:
    tid = await _negocio()
    cid = await _conexion(tid)
    conv = await _conversacion(tid, cid, "Ana Compradora")
    h = await api_headers(app_client, tid)

    r = await app_client.get(_URL, headers=h)

    assert r.status_code == 200, r.text
    fila = next(x for x in r.json()["recent"] if x["id"] == str(conv))
    assert fila["customer_name"] == "Ana Compradora"
    assert fila["channel"] == "web"
    cuando = datetime.fromisoformat(fila["started_at"])
    assert abs(datetime.now(UTC) - cuando) < timedelta(minutes=5)


async def test_recientes_no_mezclan_negocios(app_client) -> None:
    ajeno = await _negocio()
    await _conversacion(ajeno, await _conexion(ajeno), "Cliente Ajeno")
    propio = await _negocio()
    mia = await _conversacion(propio, await _conexion(propio), "Cliente Propio")
    h = await api_headers(app_client, propio)

    r = await app_client.get(_URL, headers=h)

    nombres = {x["customer_name"] for x in r.json()["recent"]}
    assert nombres == {"Cliente Propio"}
    assert str(mia) in {x["id"] for x in r.json()["recent"]}
