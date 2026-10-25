"""HU-CFG-03 (SPEC-37): `PUT /identity` rechaza con 422 lo que el registro no acepta y no escribe nada."""

from __future__ import annotations

import os

import pytest
from _api_helpers import api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/settings"


async def _negocio():
    from core.alta_negocio import onboard_tenant

    slug = uniq("cfg03")
    tid, _ = await onboard_tenant(name=f"N {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local")
    return tid


async def _identidad(app_client, headers) -> dict:
    r = await app_client.get(URL, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["identity"]


FUERA_DE_RANGO = [
    ("guardrail_max_retries", 6),
    ("difficulty_long_message_threshold", 49),
    ("difficulty_turnos_sin_resolver_threshold", 21),
    ("difficulty_score_threshold", 11),
    ("auto_escalate_after_turns", 21),
    ("persona_warmth", 6),
    ("persona_max_length", 10001),
]


async def test_fuera_de_rango_responde_422_con_el_campo_y_no_escribe(app_client) -> None:
    headers = await api_headers(app_client, await _negocio())
    antes = await _identidad(app_client, headers)
    for campo, valor in FUERA_DE_RANGO:
        r = await app_client.put(f"{URL}/identity", headers=headers, json={**antes, campo: valor})
        assert r.status_code == 422, (campo, r.text)
        assert campo in r.json()["error"]["fields"], (campo, r.text)
    assert await _identidad(app_client, headers) == antes


async def test_valor_desconocido_responde_422_con_la_lista_valida(app_client) -> None:
    headers = await api_headers(app_client, await _negocio())
    antes = await _identidad(app_client, headers)
    r = await app_client.put(f"{URL}/identity", headers=headers, json={**antes, "debounce_mode": "xyz"})
    assert r.status_code == 422, r.text
    mensaje = r.json()["error"]["fields"]["debounce_mode"]
    assert "inmediato, corto, rapido, natural, pausado" in mensaje
    assert (await _identidad(app_client, headers))["debounce_mode"] == antes["debounce_mode"]


async def test_en_rango_y_valor_valido_guardan_y_son_por_negocio(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    antes_b = await _identidad(app_client, hb)
    ident_a = await _identidad(app_client, ha)
    r = await app_client.put(
        f"{URL}/identity",
        headers=ha,
        json={**ident_a, "debounce_mode": "corto", "difficulty_score_threshold": 10, "guardrail_max_retries": 5},
    )
    assert r.status_code == 200, r.text
    guardado = r.json()
    assert (guardado["debounce_mode"], guardado["difficulty_score_threshold"], guardado["guardrail_max_retries"]) == ("corto", 10, 5)
    assert await _identidad(app_client, hb) == antes_b


async def test_lectura_tolerante_con_una_fila_vieja_fuera_de_rango(app_client) -> None:
    """Un `behavior_config` escrito por un script con valores absurdos no tumba la lectura de la API."""
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid), get_session() as s:
        fila = (await s.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one()
        fila.behavior_config = {**dict(fila.behavior_config or {}), "difficulty_score_threshold": 999}
    headers = await api_headers(app_client, tid)
    r = await app_client.get(URL, headers=headers)
    assert r.status_code == 200, r.text
