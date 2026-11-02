"""HU-CFG-09: la configuración de Cobranza no muestra las claves que el runtime no lee."""

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

OCULTAS = ("asistente_activo", "escalar_siempre_a_humano")
RUTA = "/api/v1/admin/plugins/pack_cobranza/config"


async def test_la_config_de_cobranza_no_trae_las_claves_ocultas_y_guardar_las_conserva(app_client) -> None:
    from core.alta_negocio import onboard_tenant

    slug = uniq("cfg-oculta")
    tid, _ = await onboard_tenant(
        name=f"Oculta {slug}",
        slug=slug,
        business_description="Test.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    headers = await api_headers(app_client, tid)

    r = await app_client.get(RUTA, headers=headers)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert not set(OCULTAS) & set(cuerpo["schema"]["properties"])
    assert not set(OCULTAS) & set(cuerpo["values"])
    assert "umbral_mora_dias" in cuerpo["schema"]["properties"]

    # Un cliente viejo que las mande no rompe el guardado, y la respuesta sigue sin traerlas.
    r = await app_client.put(
        RUTA, headers=headers, json={"values": {"umbral_mora_dias": 7, "escalar_siempre_a_humano": True}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["values"]["umbral_mora_dias"] == 7
    assert not set(OCULTAS) & set(r.json()["values"])
