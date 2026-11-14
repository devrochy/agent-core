"""ADR-0194 §2 · Importar el catálogo con mapeo de IA y precarga en el cliente, también en producción.

- **Mapear:** un CSV con cabeceras propias se mapea por sinónimos y, lo que no se reconoce, con la IA
  (proveedor falso). Devuelve filas normalizadas y vista previa; no escribe datos, solo la fila del ledger
  (`generacion`, `proposito = 'mapeo_columnas'`).
- **Precarga:** el cliente edita las filas y las confirma en JSON; se vuelven a validar (422 con errores
  salvo `solo_validas`) y la importación es idempotente por SKU.
- **Producción:** sin `QA_TESTERS_ENABLED` funciona para `productos`; un agente de otro negocio es 404.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from _api_helpers import api_headers
from _ayudas_sql import _sql
from test_qa_datos_generados import _agente, _negocio

pytestmark = [
    pytest.mark.aislamiento,  # agente de otro negocio y compuerta de producción (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_BASE = "/api/v1/admin/agents"

HOJA = (
    "Cod. interno;Producto;PVP final;Existencias;Proveedor\r\n"
    "CERA-1;Cera mate;28.000;12;Acme\r\n"
    "ACE-2;Aceite para barba;35000;;Acme\r\n"
    ";Sin código;1000;1;Acme\r\n"
)


class IAFalsa:
    model = "falsa"

    def __init__(self, respuesta: dict[str, Any]) -> None:
        self.respuesta = respuesta
        self.llamadas = 0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMResponse, LLMUsageInfo

        self.llamadas += 1
        entrada = json.loads(kwargs["messages"][0].content)
        assert entrada["cabeceras_sin_reconocer"] == ["Cod. interno", "PVP final", "Proveedor"]
        return LLMResponse(
            content=json.dumps(self.respuesta),
            stop_reason="end_turn",
            usage=LLMUsageInfo(input_tokens=300, output_tokens=40),
            model="falsa",
            latency_ms=3,
        )

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0002


def _ia(monkeypatch) -> IAFalsa:
    from core.qa import mapeo_columnas

    ia = IAFalsa(
        {
            "asignaciones": [
                {"cabecera": "Cod. interno", "columna": "sku"},
                {"cabecera": "PVP final", "columna": "precio"},
                {"cabecera": "Proveedor", "columna": None},
            ]
        }
    )
    monkeypatch.setattr(mapeo_columnas, "_proveedor", lambda: ia)
    return ia


@pytest.fixture
def produccion(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", False)


async def test_mapear_editar_y_confirmar_en_produccion(app_client, monkeypatch, produccion) -> None:
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    ia = _ia(monkeypatch)

    r = await app_client.post(
        f"{_BASE}/{ventas}/importar-datos/mapear",
        headers=h,
        files={"archivo": ("hoja.csv", HOJA.encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200, r.text
    propuesta = r.json()
    assert ia.llamadas == 1
    assert [(m["cabecera"], m["columna"], m["origen"]) for m in propuesta["mapeo"]] == [
        ("Cod. interno", "sku", "ia"),
        ("Producto", "nombre", "heuristica"),
        ("PVP final", "precio", "ia"),
        ("Existencias", "stock", "heuristica"),
        ("Proveedor", None, "ninguno"),
    ]
    assert propuesta["columnas_faltantes"] == [] and propuesta["ia"] == {"usada": True, "motivo_fallo": None}
    assert propuesta["filas"][0] == {
        "fila": 2,
        "valores": {"sku": "CERA-1", "nombre": "Cera mate", "precio": "28.000", "stock": "12"},
    }
    previa = propuesta["vista_previa"]
    assert (previa["nuevas"], previa["con_errores"]) == (2, 1)
    assert previa["errores"][0]["fila"] == 4 and previa["errores"][0]["columna"] == "sku"
    # Mapear no escribe datos; solo mide la llamada.
    assert (await _sql(tid, "select count(*) from pack_commerce_products where tenant_id = :t", t=tid))[0][0] == 0
    ledger = await _sql(
        tid,
        "select result->>'proposito', result->>'plantilla', credits > 0 from usage_ledger "
        "where tenant_id = :t and kind = 'generacion'",
        t=tid,
    )
    assert ledger == [("mapeo_columnas", "productos", True)]

    # El cliente completa el SKU que faltaba y confirma.
    filas = propuesta["filas"]
    filas[2]["valores"]["sku"] = "SC-3"
    previa = await app_client.post(f"{_BASE}/{ventas}/importar-datos/filas/validar", headers=h, json={"filas": filas})
    assert previa.status_code == 200 and (previa.json()["nuevas"], previa.json()["con_errores"]) == (3, 0)
    r = await app_client.post(f"{_BASE}/{ventas}/importar-datos/filas", headers=h, json={"filas": filas})
    assert r.status_code == 201, r.text
    assert r.json()["creadas"] == 3
    skus = await _sql(tid, "select sku from pack_commerce_product_variants where tenant_id = :t order by sku", t=tid)
    assert [s for (s,) in skus] == ["ACE-2", "CERA-1", "SC-3"]
    fuente = await _sql(
        tid, "select input->>'fuente' from audit_log where tenant_id = :t and action = 'qa_importar_datos'", t=tid
    )
    assert fuente == [("precarga",)]

    # Confirmar otra vez no duplica (idempotente por SKU).
    r = await app_client.post(f"{_BASE}/{ventas}/importar-datos/filas", headers=h, json={"filas": filas})
    assert r.status_code == 201 and (r.json()["creadas"], r.json()["existentes"]) == (0, 3)


async def test_precarga_con_errores_se_revalida(app_client, produccion) -> None:
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    filas = [
        {"fila": 2, "valores": {"sku": "A-1", "nombre": "Cera", "precio": "abc", "color": "rojo"}},
        {"fila": 3, "valores": {"sku": "B-2", "nombre": "Aceite", "precio": "1000"}},
    ]
    r = await app_client.post(f"{_BASE}/{ventas}/importar-datos/filas", headers=h, json={"filas": filas})
    assert r.status_code == 422 and r.json()["error"]["code"] == "archivo_con_errores"
    previa = (
        await app_client.post(f"{_BASE}/{ventas}/importar-datos/filas/validar", headers=h, json={"filas": filas})
    ).json()
    assert previa["columnas_ignoradas"] == ["color"] and previa["errores"][0]["columna"] == "precio"
    r = await app_client.post(
        f"{_BASE}/{ventas}/importar-datos/filas", headers=h, json={"filas": filas, "solo_validas": True}
    )
    assert r.status_code == 201 and r.json()["creadas"] == 1
    largo = [{"fila": 2, "valores": {"sku": "X", "nombre": "a" * 3000, "precio": "1"}}]
    r = await app_client.post(f"{_BASE}/{ventas}/importar-datos/filas/validar", headers=h, json={"filas": largo})
    assert r.status_code == 422


async def test_otro_negocio_y_plantillas_solo_de_test(app_client, monkeypatch, produccion) -> None:
    mio = await _negocio()
    suyo = await _negocio()
    suyo_ventas = await _agente(suyo, "ventas", ["pack_commerce.tomar_pedido"])
    mio_agenda = await _agente(mio, "agenda", ["pack_calendar.agendar_cita"])
    h = await api_headers(app_client, mio)
    _ia(monkeypatch)
    filas = {"filas": [{"fila": 2, "valores": {"sku": "A", "nombre": "Cera", "precio": "1"}}]}

    for agente in (suyo_ventas, mio_agenda):
        r = await app_client.post(
            f"{_BASE}/{agente}/importar-datos/mapear",
            headers=h,
            files={"archivo": ("hoja.csv", HOJA.encode("utf-8"), "text/csv")},
        )
        assert r.status_code == 404 and str(agente) not in r.text
        for ruta in ("/filas/validar", "/filas"):
            r = await app_client.post(f"{_BASE}/{agente}/importar-datos{ruta}", headers=h, json=filas)
            assert r.status_code == 404 and str(agente) not in r.text
    for tid in (mio, suyo):
        assert (await _sql(tid, "select count(*) from pack_commerce_products where tenant_id = :t", t=tid))[0][0] == 0
        assert (await _sql(tid, "select count(*) from usage_ledger where tenant_id = :t and kind = 'generacion'", t=tid))[0][0] == 0
