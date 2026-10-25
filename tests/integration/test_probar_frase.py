"""HU-AOS-26 · probar una frase contra el router de una conexión (sin LLM, sin escribir nada).

Con Ventas (por defecto) y Agenda publicados: "quiero agendar una demo" la atiende Agenda por
disparador; un saludo, Ventas por defecto. Sin release activa, 409; la conexión de otro negocio,
404. Con el enrutamiento apagado, igual se simula y `mode` lo dice.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers
from test_enrutamiento_especialistas import CITA, _con_dos_especialistas, _modules, _negocio, _sql

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/a2a/route-preview"


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.tools.core_skill import register_core_skills

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()
    await plugin_manager.ensure_process_activation()
    register_core_skills(mcp_host)


@pytest.fixture
def _ruteo(monkeypatch: Any) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def test_la_frase_va_al_agente_del_disparador_y_el_saludo_al_por_defecto(_ruteo: None, app_client) -> None:
    tid, cid, _inst = await _con_dos_especialistas("active")
    h = await api_headers(app_client, tid)
    antes = (await _sql(tid, "select count(*) from core_decisions"))[0][0]
    r = await app_client.post(URL, headers=h, json={"connection_id": str(cid), "text": "quiero agendar una demo"})
    assert r.status_code == 200, r.text
    datos = r.json()
    assert (datos["mode"], datos["source"], datos["agent_key"], datos["procedure_id"]) == ("active", "trigger", "agenda", CITA)
    assert datos["confidence"] >= 0.8 and datos["candidates"][0]["owner"] == "agenda"
    r = await app_client.post(URL, headers=h, json={"connection_id": str(cid), "text": "hola"})
    assert (r.json()["source"], r.json()["agent_key"]) == ("default", "ventas")
    # No escribe nada en el ledger del turno.
    assert (await _sql(tid, "select count(*) from core_decisions"))[0][0] == antes


async def test_con_el_enrutamiento_apagado_se_simula_y_lo_dice(app_client) -> None:
    tid, cid, _inst = await _con_dos_especialistas("active")
    r = await app_client.post(URL, headers=await api_headers(app_client, tid), json={"connection_id": str(cid), "text": "quiero agendar una demo"})
    assert r.status_code == 200 and r.json()["mode"] == "off" and r.json()["agent_key"] == "agenda"


async def test_sin_release_activa_y_de_otro_negocio(_ruteo: None, app_client) -> None:
    tid, _cid = await _negocio()
    _otro, otra_conexion, _ = await _con_dos_especialistas("active")
    nueva = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=nueva, t=tid, ext=f"ig-{uuid.uuid4().hex[:10]}",
    )
    h = await api_headers(app_client, tid)
    r = await app_client.post(URL, headers=h, json={"connection_id": str(nueva), "text": "hola"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "sin_release_activa", r.text
    r = await app_client.post(URL, headers=h, json={"connection_id": str(otra_conexion), "text": "hola"})
    assert r.status_code == 404
    r = await app_client.post(URL, headers=h, json={"connection_id": str(nueva), "text": ""})
    assert r.status_code == 422
