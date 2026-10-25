"""HU-SUB-25 · `GET /api/v1/pack/commerce/pago-en-linea/minimo`, contra la BD real.

El panel avisa al guardar un plan bajo el mínimo de la pasarela y no lleva la cifra: la pregunta al
servidor. Sigue el valor del negocio (si lo fijó), si no el que declara la pasarela de su ambiente;
sin ninguno no hay mínimo. También informa cuántas cuentas de transferencia tiene el negocio.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

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

RUTA = "/api/v1/pack/commerce/pago-en-linea/minimo"


class _Pasarela:
    plugin_id = "pack_payments"

    def __init__(self, minimo: float | None) -> None:
        self._minimo = minimo

    async def monto_minimo(self) -> float | None:
        return self._minimo


class _Cuentas:
    plugin_id = "pack_commerce"

    def __init__(self, n: int) -> None:
        self._n = n

    async def cuentas_de_transferencia(self) -> list[dict]:
        return [{"entity": f"Banco {i}", "accountNumber": f"{i}"} for i in range(self._n)]


async def _negocio():
    from core.alta_negocio import onboard_tenant

    slug = uniq("minimo")
    tid, _ = await onboard_tenant(
        name=f"Negocio {slug}",
        slug=slug,
        business_description="Test del mínimo.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
        packs=["pack_commerce"],
    )
    return tid


@pytest.fixture
def pasarela(monkeypatch):
    """Fija lo que la pasarela declara (o `None` = sin pasarela) y la config del pack."""
    import pack_commerce.minimo as minimo
    import pack_commerce.skills as skills

    estado = {"capacidad": None, "config": SimpleNamespace(pago_en_linea_monto_minimo=None)}

    async def _cap():
        return estado["capacidad"]

    async def _config():
        return estado["config"]

    monkeypatch.setattr(minimo, "_capacidad_de_pago", _cap)
    monkeypatch.setattr(skills, "_plugin_config", _config)
    return estado


async def test_sigue_al_ambiente_de_la_pasarela(app_client, pasarela) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)

    pasarela["capacidad"] = _Pasarela(150_000)  # sandbox
    r = await app_client.get(RUTA, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"minimo": 150000.0, "fuente": "pasarela", "moneda": "COP", "cuentas_de_transferencia": 0}

    pasarela["capacidad"] = _Pasarela(1_500)  # producción
    assert (await app_client.get(RUTA, headers=h)).json()["minimo"] == 1500.0


async def test_el_valor_del_negocio_manda_sobre_el_de_la_pasarela(app_client, pasarela) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    pasarela["capacidad"] = _Pasarela(150_000)
    pasarela["config"] = SimpleNamespace(pago_en_linea_monto_minimo=50_000)
    cuerpo = (await app_client.get(RUTA, headers=h)).json()
    assert cuerpo["minimo"] == 50000.0 and cuerpo["fuente"] == "negocio"

    pasarela["config"] = SimpleNamespace(pago_en_linea_monto_minimo=0)  # mínimo apagado
    cuerpo = (await app_client.get(RUTA, headers=h)).json()
    assert cuerpo["minimo"] == 0.0 and cuerpo["fuente"] == "negocio"


async def test_sin_pasarela_no_hay_minimo(app_client, pasarela) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    cuerpo = (await app_client.get(RUTA, headers=h)).json()
    assert cuerpo["minimo"] is None and cuerpo["fuente"] == "ninguna"

    pasarela["capacidad"] = _Pasarela(None)  # pasarela sin configurar
    assert (await app_client.get(RUTA, headers=h)).json()["minimo"] is None


async def test_informa_cuantas_cuentas_de_transferencia_tiene(app_client, pasarela) -> None:
    from core.payments.transferencia import transfer_registry

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    assert (await app_client.get(RUTA, headers=h)).json()["cuentas_de_transferencia"] == 0
    original = transfer_registry._providers.get("pack_commerce")
    transfer_registry.register("pack_commerce", _Cuentas(2))
    try:
        assert (await app_client.get(RUTA, headers=h)).json()["cuentas_de_transferencia"] == 2
    finally:
        transfer_registry.unregister("pack_commerce")
        if original is not None:
            transfer_registry.register("pack_commerce", original)


async def test_sin_sesion_o_sin_permiso_no_responde(app_client, pasarela) -> None:
    tid = await _negocio()
    assert (await app_client.get(RUTA, headers={"X-Tenant-Id": str(tid)})).status_code in (401, 403)


async def test_la_config_de_un_negocio_no_se_ve_desde_otro(app_client, monkeypatch) -> None:
    """Sin falsear la config ni el registro de pasarelas: el mínimo y las cuentas que fijó A no
    llegan a B. Corre el camino real (config del pack bajo RLS, spec activo, registros)."""
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    a, b = await _negocio(), await _negocio()
    async with tenant_scope(a):
        await plugin_manager.save_config(
            "pack_commerce",
            {
                "pago_en_linea_monto_minimo": 77_000,
                "transfer_accounts": [
                    {"entity": "Nequi", "accountType": "ahorros", "accountNumber": "3001112233", "holderName": "A", "holderDocument": "1"}
                ],
            },
        )
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)

    de_a = (await app_client.get(RUTA, headers=ha)).json()
    assert de_a["minimo"] == 77000.0 and de_a["fuente"] == "negocio" and de_a["cuentas_de_transferencia"] == 1
    de_b = (await app_client.get(RUTA, headers=hb)).json()
    assert de_b["fuente"] != "negocio" and de_b["minimo"] != 77000.0 and de_b["cuentas_de_transferencia"] == 0
    assert set(de_a) == {"minimo", "fuente", "moneda", "cuentas_de_transferencia"}  # solo un conteo, nunca las cuentas
