"""HU-QA-10 · Datos de prueba por agente según la descripción del negocio (ADR-0146).

- **Generar:** el agente de ventas recibe el catálogo que devuelve el LLM (validado), con SKUs del
  lote, y la llamada queda en `usage_ledger` como `generacion`.
- **Regenerar sin duplicar:** el lote anterior se retira (variantes inactivas, categorías borradas)
  y lo creado a mano sigue igual.
- **Borrar:** retira solo lo sembrado; la cartera queda con `deleted_at`.
- **Respaldo:** salida inválida o proveedor caído → plantilla del pack, con el motivo.
- **Sin descripción** (o la fija de «Preparar mis datos») → 422; **General** → no disponible;
  **sin `QA_TESTERS_ENABLED`** → 404; **agente de otro negocio** → 404 sin eco.

Sin LLM real: el proveedor es un doble que devuelve JSON guionado (regla de costo del dueño).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from test_backfill_agentes import _sql

pytestmark = [
    pytest.mark.aislamiento,  # agente de otro negocio y bandera de entorno (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_BASE = "/api/v1/admin/agents"
DESCRIPCION = "Barbería en Medellín: cortes, arreglo de barba y productos para el cabello."

CATALOGO = {
    "category_paths": [["Cortes"], ["Productos"]],
    "products": [
        {"name": "Corte clásico", "category_path": ["Cortes"], "variants": [{"price": 30000}]},
        {"name": "Arreglo de barba", "category_path": ["Cortes"], "variants": [{"price": 20000}]},
        {
            "name": "Cera para el cabello",
            "category_path": ["Productos"],
            "variants": [{"price": 35000, "stock": 8, "attributes": {"fijación": "fuerte"}}],
            "sku": "lo-ignora",
        },
    ],
}


class ProveedorGuionado:
    """Doble del LLM: devuelve `contenido` o lanza `error`."""

    provider_name = "guionado"
    model = "guionado"

    def __init__(self, contenido: str = "", error: Exception | None = None) -> None:
        self.contenido = contenido
        self.error = error
        self.llamadas = 0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMResponse, LLMUsageInfo

        self.llamadas += 1
        assert "esquema" in kwargs["messages"][0].content
        if self.error is not None:
            raise self.error
        return LLMResponse(
            content=self.contenido,
            stop_reason="end_turn",
            usage=LLMUsageInfo(input_tokens=1200, output_tokens=800),
            model="guionado",
            latency_ms=5,
        )

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.001


@pytest.fixture
def qa_on(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)


def _proveedor(monkeypatch, proveedor: ProveedorGuionado) -> ProveedorGuionado:
    from core.qa import datos_generados

    monkeypatch.setattr(datos_generados, "_proveedor", lambda: proveedor)
    return proveedor


async def _negocio(descripcion: str = DESCRIPCION) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("qa10")
    tid, _ = await onboard_tenant(
        name=f"QA10 {slug}",
        slug=slug,
        business_description=descripcion,
        packs=["pack_commerce", "pack_calendar", "pack_cobranza"],
        admin_email="admin@local",
        sembrar_ejemplos=False,
    )
    return tid


async def _agente(tid: uuid.UUID, key: str, procedimientos: list[str], kind: str = "specialist") -> uuid.UUID:
    aid = uuid.uuid4()
    await _sql(
        tid,
        "insert into agents (id, tenant_id, key, kind, name) values (:id, :t, :k, :kind, :n)",
        id=aid, t=tid, k=key, kind=kind, n=key.capitalize(),
    )
    contenido = {"contract": "agentspec/3", "procedures": [{"id": p} for p in procedimientos]}
    await _sql(
        tid,
        "insert into agent_versions (id, tenant_id, agent_id, version, label, contract, content, content_hash) "
        "values (:id, :t, :a, 1, 'v1', 'agentspec/3', cast(:c as jsonb), :h)",
        id=uuid.uuid4(), t=tid, a=aid, c=json.dumps(contenido), h=uuid.uuid4().hex + uuid.uuid4().hex,
    )
    return aid


async def _variantes(tid: uuid.UUID) -> dict[str, bool]:
    filas = await _sql(tid, "select sku, active from pack_commerce_product_variants where tenant_id = :t", t=tid)
    return {sku: activa for sku, activa in filas}


# ---------------------------------------------------------------- generar, regenerar, borrar
async def test_generar_regenerar_y_borrar_catalogo(app_client, monkeypatch, qa_on) -> None:
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    proveedor = _proveedor(monkeypatch, ProveedorGuionado("Aquí van:\n" + json.dumps(CATALOGO)))

    r = await app_client.get(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["disponible"] is True and r.json()["forma"]["modo"] == "catalogo"
    assert r.json()["lote"] is None

    # Un producto creado a mano: no es de la prueba y nunca se toca.
    await _sql(
        tid,
        "insert into pack_commerce_products (id, tenant_id, name, description) values (:id, :t, 'Mío', '')",
        id=(pid := uuid.uuid4()), t=tid,
    )
    await _sql(
        tid,
        "insert into pack_commerce_product_variants (id, tenant_id, product_id, sku, attributes, price, active) "
        "values (:id, :t, :p, 'MANUAL-1', '{}'::jsonb, 1000, true)",
        id=uuid.uuid4(), t=tid, p=pid,
    )

    r = await app_client.post(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)
    assert r.status_code == 201, r.text
    lote = r.json()
    assert lote["origen"] == "llm" and lote["motivo_plantilla"] is None
    assert lote["conteos"] == {"categoria": 2, "producto": 3, "variante": 3}
    variantes = await _variantes(tid)
    primeras = {s for s in variantes if s.startswith("PRUEBA-")}
    assert len(primeras) == 3 and all(variantes[s] for s in primeras)
    assert "lo-ignora" not in variantes
    ledger = await _sql(
        tid, "select credits, key_mode from usage_ledger where tenant_id = :t and kind = 'generacion'", t=tid
    )
    assert len(ledger) == 1 and ledger[0][1] == "platform" and ledger[0][0] is not None

    r = await app_client.post(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["retirados"] == 5  # 3 variantes + 2 categorías
    variantes = await _variantes(tid)
    assert not any(variantes[s] for s in primeras)
    segundas = {s for s in variantes if s.startswith("PRUEBA-")} - primeras
    assert len(segundas) == 3 and all(variantes[s] for s in segundas)
    assert variantes["MANUAL-1"] is True
    vivas = await _sql(
        tid,
        "select count(*) from pack_commerce_categories where tenant_id = :t and deleted_at is null "
        "and name in ('Cortes', 'Productos')",
        t=tid,
    )
    assert vivas[0][0] == 2  # las del segundo lote, no duplicadas

    r = await app_client.get(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)
    assert r.json()["lote"]["conteos"]["variante"] == 3

    r = await app_client.delete(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)
    assert r.status_code == 200 and r.json()["retirados"] == 5
    variantes = await _variantes(tid)
    assert not any(variantes[s] for s in segundas) and variantes["MANUAL-1"] is True
    assert (await app_client.get(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).json()["lote"] is None
    assert proveedor.llamadas == 2


async def test_cartera_se_retira_con_borrado_logico(app_client, monkeypatch, qa_on) -> None:
    tid = await _negocio("Financiera de microcréditos para tenderos en Cali.")
    cobranza = await _agente(tid, "cobranza", ["pack_cobranza.registrar_promesa"])
    h = await api_headers(app_client, tid)
    cartera = {
        "carteras": [
            {
                "nombre": "Tenderos Sur",
                "tipo_interes": 20,
                "num_cuotas": 10,
                "saldo_caja": 2000000,
                "clientes": [
                    {"nombre": "Ana", "apellido": "Ríos", "numero_documento": "10203040", "telefono": "3001234567",
                     "prestamo": {"valor": 200000, "num_cuotas": 10, "tipo_interes": 20, "dias_entre_cuotas": 1,
                                  "hace_dias": 8, "cuotas_pagadas": 3}},
                    {"nombre": "Luis", "apellido": "Mora", "numero_documento": "10203041",
                     "prestamo": {"valor": 100000, "num_cuotas": 5, "tipo_interes": 10, "dias_entre_cuotas": 2,
                                  "hace_dias": 2, "cuotas_pagadas": 1}},
                    {"nombre": "Eva", "apellido": "Paz", "numero_documento": "10203042"},
                ],
            }
        ]
    }
    _proveedor(monkeypatch, ProveedorGuionado(json.dumps(cartera)))

    r = await app_client.post(f"{_BASE}/{cobranza}/datos-de-prueba", headers=h)
    assert r.status_code == 201, r.text
    conteos = r.json()["conteos"]
    assert conteos["cartera"] == 1 and conteos["cliente"] == 3 and conteos["prestamo"] == 2
    clientes = await _sql(
        tid, "select telefono_whatsapp from pack_cobranza_clientes where tenant_id = :t and deleted_at is null", t=tid
    )
    assert len(clientes) == 3 and all(c[0] is None for c in clientes)  # el teléfono del LLM se descarta
    nombre = await _sql(tid, "select nombre from pack_cobranza_carteras where tenant_id = :t", t=tid)
    assert nombre[0][0].startswith("Tenderos Sur · ")

    r = await app_client.delete(f"{_BASE}/{cobranza}/datos-de-prueba", headers=h)
    assert r.status_code == 200
    vivos = await _sql(
        tid,
        "select (select count(*) from pack_cobranza_clientes where tenant_id = :t and deleted_at is null), "
        "(select count(*) from pack_cobranza_cuotas where tenant_id = :t and deleted_at is null), "
        "(select count(*) from pack_cobranza_clientes where tenant_id = :t)",
        t=tid,
    )
    assert vivos[0] == (0, 0, 3)  # retirados, no borrados


# ---------------------------------------------------------------- respaldo
async def test_salida_invalida_usa_la_plantilla(app_client, monkeypatch, qa_on) -> None:
    tid = await _negocio()
    agenda = await _agente(tid, "agenda", ["pack_calendar.agendar_cita", "pack_commerce.crear_lead"])
    h = await api_headers(app_client, tid)
    _proveedor(monkeypatch, ProveedorGuionado('{"tipos": []}'))

    estado = (await app_client.get(f"{_BASE}/{agenda}/datos-de-prueba", headers=h)).json()
    assert estado["forma"]["modo"] == "agenda"  # la agenda gana sobre el comercio que usa para leads
    r = await app_client.post(f"{_BASE}/{agenda}/datos-de-prueba", headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["origen"] == "plantilla" and r.json()["motivo_plantilla"] == "formato"
    assert r.json()["conteos"]["tipo"] >= 1


async def test_proveedor_caido_usa_la_plantilla(app_client, monkeypatch, qa_on) -> None:
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    _proveedor(monkeypatch, ProveedorGuionado(error=RuntimeError("402 sin saldo")))

    r = await app_client.post(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["origen"] == "plantilla" and r.json()["motivo_plantilla"] == "proveedor"
    assert r.json()["conteos"]["variante"] >= 3


# ---------------------------------------------------------------- reglas
async def test_sin_descripcion_general_bandera_y_aislamiento(app_client, monkeypatch, qa_on) -> None:
    from core.config import get_settings
    from core.qa.datos import DESCRIPCION_QA

    _proveedor(monkeypatch, ProveedorGuionado(json.dumps(CATALOGO)))
    tid = await _negocio(DESCRIPCION_QA)
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    general = await _agente(tid, "general", [], kind="core")
    h = await api_headers(app_client, tid)

    estado = (await app_client.get(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).json()
    assert estado["falta_descripcion"] is True
    r = await app_client.post(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "descripcion_requerida", r.text

    estado = (await app_client.get(f"{_BASE}/{general}/datos-de-prueba", headers=h)).json()
    assert estado["disponible"] is False and estado["motivo"]
    assert (await app_client.post(f"{_BASE}/{general}/datos-de-prueba", headers=h)).status_code == 409

    otro = await _negocio()
    ajeno = await _agente(otro, "ventas", ["pack_commerce.tomar_pedido"])
    r = await app_client.post(f"{_BASE}/{ajeno}/datos-de-prueba", headers=h)
    assert r.status_code == 404 and str(ajeno) not in r.text

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", False)
    assert (await app_client.get(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).status_code == 404
    assert (await app_client.post(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).status_code == 404
    assert (await app_client.delete(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).status_code == 404


async def test_suscripciones_usa_los_planes_que_nombra_su_plantilla(app_client, monkeypatch, qa_on) -> None:
    from core.alta_negocio import onboard_tenant

    slug = uniq("qa10s")
    tid, _ = await onboard_tenant(
        name=f"QA10 {slug}",
        slug=slug,
        business_description="Gimnasio de barrio con planes mensuales y anuales.",
        packs=["pack_commerce", "pack_subscriptions"],
        admin_email="admin@local",
        sembrar_ejemplos=False,
    )
    agente = await _agente(tid, "suscripciones", ["pack_subscriptions.cambiar_plan"])
    h = await api_headers(app_client, tid)
    planes = {
        "products": [
            {"name": "Plan mensual", "variants": [{"price": 90000, "attributes": {"periodicidad": "mensual"}}]},
            {"name": "Plan anual", "variants": [{"price": 900000, "attributes": {"periodicidad": "anual", "trial_dias": 7}}]},
        ]
    }
    _proveedor(monkeypatch, ProveedorGuionado(json.dumps(planes)))

    estado = (await app_client.get(f"{_BASE}/{agente}/datos-de-prueba", headers=h)).json()
    assert estado["disponible"] is True and estado["forma"]["modo"] == "planes", estado
    r = await app_client.post(f"{_BASE}/{agente}/datos-de-prueba", headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["origen"] == "llm" and r.json()["conteos"]["variante"] == 2
    filas = await _sql(
        tid,
        "select v.billing_period, v.trial_days from pack_commerce_product_variants v "
        "where v.tenant_id = :t and v.sku like 'PRUEBA-%' order by v.price",
        t=tid,
    )
    assert filas == [("mensual", 0), ("anual", 7)]
