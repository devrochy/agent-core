"""Config de Captar leads desde el panel (ADR-0193 §6, HU-AOS-70 parte 3): `/api/v1/admin/captar-leads/config`.

Los secretos del intake no salen nunca; guardarlos vacíos conserva los guardados; la config de commerce ya no trae
los campos de leads (quedan ocultos solo para la copia); y cada negocio ve solo la suya.
"""

from __future__ import annotations

import os

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RUTA = "/api/v1/admin/captar-leads/config"
SECRETOS = ("lead_intake_secret", "lead_intake_turnstile")


async def _negocio(packs: list[str] | None = None):
    from _habilidades import onboard_tenant

    slug = uniq("captar")
    tid, _ = await onboard_tenant(
        name=f"Captar {slug}", slug=slug, persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL,
        packs=packs or [], sembrar_ejemplos=False,
    )
    return tid, slug


async def test_leer_y_guardar_sin_exponer_secretos(app_client) -> None:
    tid, slug = await _negocio()
    headers = await api_headers(app_client, tid)

    r = await app_client.get(RUTA, headers=headers)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["configured"] is False and cuerpo["configured_secrets"] == []
    assert cuerpo["values"]["lead_intake_enabled"] is False
    assert "lead_intake_enabled" in cuerpo["schema"]["properties"]
    assert not set(SECRETOS) & set(cuerpo["values"])

    valores = {
        "lead_intake_enabled": True,
        "lead_intake_secret": "s3cr3t",
        "lead_intake_turnstile": "0xLLAVE",
        "lead_intake_allowed_origins": ["https://www.ejemplo.co"],
        "follow_up_hours": 6,
    }
    r = await app_client.put(RUTA, headers=headers, json={"values": valores})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["configured"] is True
    assert sorted(cuerpo["configured_secrets"]) == sorted(SECRETOS)
    assert "s3cr3t" not in r.text and "0xLLAVE" not in r.text
    assert cuerpo["values"]["follow_up_hours"] == 6

    # El formulario manda los secretos vacíos: se conservan. El intake público ya responde con ellos.
    r = await app_client.put(
        RUTA, headers=headers, json={"values": {**valores, "lead_intake_secret": "", "lead_intake_turnstile": ""}}
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["configured_secrets"]) == sorted(SECRETOS)
    r = await app_client.get(f"/api/public/leads/intake-config/{slug}")
    assert r.json() == {"enabled": True, "allowed_origins": ["https://www.ejemplo.co"], "turnstile": True}

    # Fuera de rango: 422 por campo y no se guarda nada.
    r = await app_client.put(RUTA, headers=headers, json={"values": {**valores, "seguimiento_resumen_hora": 30}})
    assert r.status_code == 422, r.text
    assert "seguimiento_resumen_hora" in r.json()["error"]["fields"]
    assert (await app_client.get(RUTA, headers=headers)).json()["values"]["seguimiento_resumen_hora"] == 8


async def test_cada_negocio_ve_solo_su_config(app_client) -> None:
    a, _ = await _negocio()
    b, _ = await _negocio()
    r = await app_client.put(
        RUTA, headers=await api_headers(app_client, a), json={"values": {"lead_intake_enabled": True}}
    )
    assert r.status_code == 200, r.text
    r = await app_client.get(RUTA, headers=await api_headers(app_client, b))
    assert r.status_code == 200
    assert r.json()["configured"] is False and r.json()["values"]["lead_intake_enabled"] is False


async def test_la_config_de_commerce_ya_no_muestra_los_campos_de_leads(app_client) -> None:
    from core.leads.config import ConfigCaptarLeads
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid, _ = await _negocio(["pack_commerce"])
    headers = await api_headers(app_client, tid)
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_commerce", {"lead_intake_enabled": True, "lead_intake_secret": "viejo"})

    ruta = "/api/v1/admin/plugins/pack_commerce/config"
    cuerpo = (await app_client.get(ruta, headers=headers)).json()
    de_leads = set(ConfigCaptarLeads.model_fields) | {"auto_create_lead", "lead_intake_source"}
    assert not de_leads & set(cuerpo["schema"]["properties"])
    assert not de_leads & set(cuerpo["values"])

    # Guardar la config de commerce desde el panel conserva los campos ocultos (los lee `copiar-leads`).
    r = await app_client.put(ruta, headers=headers, json={"values": cuerpo["values"]})
    assert r.status_code == 200, r.text
    async with tenant_scope(tid):
        config = await plugin_manager.load_config("pack_commerce")
    assert config.lead_intake_enabled and config.lead_intake_secret.get_secret_value() == "viejo"
