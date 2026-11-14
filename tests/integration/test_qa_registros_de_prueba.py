"""HU-QA-16 · Lo cargado para probar el agente, en palabras (`describir_registros`, ADR-0194 en curso).

- **Catálogo:** el lote generado sale con nombre, precio y categoría; lo importado se suma con su
  origen; regenerar cambia solo lo generado y borrar lo importado lo saca de la lista.
- **Suscripciones:** los planes con precio, periodicidad y prueba; las suscripciones con cliente, plan y estado.
- **Agenda y cartera:** los servicios con su duración y los clientes con su cartera y deuda; las
  variantes, citas, cuotas y pagos no salen como filas.
- **Aislamiento:** agente de otro negocio → 404 sin eco; el hook del pack con el negocio equivocado no
  devuelve nada (RLS y filtro por `tenant_id`); la sección del pack que el usuario no ve no sale;
  **sin `QA_TESTERS_ENABLED`** → 404.

Sin LLM real: el proveedor es el doble guionado de HU-QA-10 (regla de costo del dueño).
"""

from __future__ import annotations

import json
import os
import re

import pytest
from _api_helpers import api_headers, uniq
from _ayudas_sql import _sql
from test_fiabilidad_api import PASSWORD, _colaborador
from test_qa_datos_generados import (
    CATALOGO,
    ProveedorGuionado,
    _agente,
    _negocio,
    _proveedor,
    qa_on,  # noqa: F401  (fixture)
)

pytestmark = [
    pytest.mark.aislamiento,  # agente de otro negocio, RLS del hook, permisos y bandera (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_BASE = "/api/v1/admin/agents"
PRODUCTOS = (
    "sku,nombre,precio,categoria,descripcion,stock\r\n"
    "CERA-1,Cera mate,28.000,Cabello > Ceras,Fijación media,12\r\n"
    "ACE-2,Aceite para barba,35000,Barba,,8\r\n"
)


async def _registros(app_client, h, agente) -> dict:
    r = await app_client.get(f"{_BASE}/{agente}/datos-de-prueba/registros", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _importar(app_client, h, agente, texto: str) -> None:
    archivo = {"archivo": ("datos.csv", texto.encode("utf-8"), "text/csv")}
    r = await app_client.post(f"{_BASE}/{agente}/importar-datos", headers=h, files=archivo)
    assert r.status_code == 201, r.text


def _filas(cuerpo: dict) -> list[tuple[str, str, str | None, str]]:
    return [(f["tipo"], f["nombre"], f["detalle"], f["origen"]) for f in cuerpo["registros"]]


# ---------------------------------------------------------------- catálogo (Ventas)
async def test_catalogo_generado_e_importado(app_client, monkeypatch, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)

    vacio = await _registros(app_client, h, ventas)
    assert vacio == {"lote": None, "registros": [], "total": 0, "sin_detalle": 0}

    _proveedor(monkeypatch, ProveedorGuionado(json.dumps(CATALOGO)))
    assert (await app_client.post(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).status_code == 201
    cuerpo = await _registros(app_client, h, ventas)
    assert cuerpo["lote"]["modo"] == "catalogo" and cuerpo["lote"]["origen"] == "llm"
    assert cuerpo["lote"]["conteos"]["producto"] == 3
    assert _filas(cuerpo) == [
        ("producto", "Arreglo de barba", "$20.000 · Cortes", "llm"),
        ("producto", "Cera para el cabello", "$35.000 · Productos", "llm"),
        ("producto", "Corte clásico", "$30.000 · Cortes", "llm"),
    ]
    assert cuerpo["total"] == 3 and cuerpo["sin_detalle"] == 0

    # Lo importado se suma, después de lo generado y con su origen.
    await _importar(app_client, h, ventas, PRODUCTOS)
    cuerpo = await _registros(app_client, h, ventas)
    assert _filas(cuerpo)[3:] == [
        ("producto", "Aceite para barba", "$35.000 · Barba", "importacion"),
        ("producto", "Cera mate", "$28.000 · Ceras", "importacion"),
    ]

    # Regenerar (ahora con la plantilla) reemplaza solo lo generado, que pasa al final; lo importado sigue.
    _proveedor(monkeypatch, ProveedorGuionado(error=TimeoutError()))
    assert (await app_client.post(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).status_code == 201
    cuerpo = await _registros(app_client, h, ventas)
    origenes = [f[3] for f in _filas(cuerpo)]
    assert origenes[:2] == ["importacion", "importacion"] and set(origenes[2:]) == {"plantilla"}
    assert all(f[0] == "producto" and f[1] for f in _filas(cuerpo))

    # Borrar lo importado lo saca de la lista; borrar lo generado deja la lista vacía.
    assert (await app_client.delete(f"{_BASE}/{ventas}/importar-datos", headers=h)).status_code == 200
    assert {f[3] for f in _filas(await _registros(app_client, h, ventas))} == {"plantilla"}
    assert (await app_client.delete(f"{_BASE}/{ventas}/datos-de-prueba", headers=h)).status_code == 200
    assert (await _registros(app_client, h, ventas))["registros"] == []


# ---------------------------------------------------------------- agenda y cartera
async def test_agenda_y_cartera(app_client, monkeypatch, qa_on) -> None:  # noqa: F811
    tid = await _negocio()
    agenda = await _agente(tid, "agenda", ["pack_calendar.agendar_cita"])
    cobranza = await _agente(tid, "cobranza", ["pack_cobranza.registrar_promesa"])
    h = await api_headers(app_client, tid)
    _proveedor(monkeypatch, ProveedorGuionado(error=TimeoutError()))  # respaldo del pack: determinista

    assert (await app_client.post(f"{_BASE}/{agenda}/datos-de-prueba", headers=h)).status_code == 201
    cuerpo = await _registros(app_client, h, agenda)
    filas = _filas(cuerpo)
    assert filas and {f[0] for f in filas} == {"tipo"}  # citas y bloqueos no salen como filas
    assert all(f[1] and re.fullmatch(r"(\d+ h)?( ?\d+ min)?", f[2] or "") and f[2] for f in filas), filas
    assert len(filas) == cuerpo["lote"]["conteos"]["tipo"]

    assert (await app_client.post(f"{_BASE}/{cobranza}/datos-de-prueba", headers=h)).status_code == 201
    cuerpo = await _registros(app_client, h, cobranza)
    filas = _filas(cuerpo)
    assert filas and {f[0] for f in filas} == {"cliente"}  # cartera, préstamos, cuotas y pagos van en el detalle
    assert len(filas) == cuerpo["lote"]["conteos"]["cliente"]
    assert all(f[1] and f[2] for f in filas), filas
    assert any(re.search(r"préstamo \$[\d.]+, saldo \$[\d.]+", f[2]) for f in filas), filas

    # Importar a la agenda suma los servicios con su duración.
    await _importar(app_client, h, agenda, "nombre,duracion_minutos\r\nCorte largo,90\r\nBarba,20\r\n")
    assert _filas(await _registros(app_client, h, agenda))[-2:] == [
        ("tipo", "Barba", "20 min", "importacion"),
        ("tipo", "Corte largo", "1 h 30 min", "importacion"),
    ]


# ---------------------------------------------------------------- aislamiento, permisos y bandera
async def test_aislamiento_permiso_y_bandera(app_client, monkeypatch, qa_on) -> None:  # noqa: F811
    from core.config import get_settings
    from core.plugin_manager import plugin_manager
    from core.qa.contrato_datos import RegistroSembrado

    mio, suyo = await _negocio(), await _negocio()
    mio_ventas = await _agente(mio, "ventas", ["pack_commerce.tomar_pedido"])
    suyo_ventas = await _agente(suyo, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, mio)
    _proveedor(monkeypatch, ProveedorGuionado(json.dumps(CATALOGO)))
    assert (await app_client.post(f"{_BASE}/{mio_ventas}/datos-de-prueba", headers=h)).status_code == 201

    # Mi sesión con el agente del otro negocio: 404 sin eco.
    r = await app_client.get(f"{_BASE}/{suyo_ventas}/datos-de-prueba/registros", headers=h)
    assert r.status_code == 404 and str(suyo_ventas) not in r.text

    # El hook del pack, llamado con el negocio equivocado, no ve los productos del otro.
    filas = await _sql(
        mio, "select kind, record_ref from test_data_records where tenant_id = :t and deleted_at is null", t=mio
    )
    refs = [RegistroSembrado(kind=k, ref=ref) for k, ref in filas]
    pack = plugin_manager.get("pack_commerce")
    assert len(await pack.describir_registros(mio, refs)) == 3
    assert await pack.describir_registros(suyo, refs) == []

    # La sección del pack decide: sin Ventas no se ven los productos (el endpoint sí responde).
    sin_ventas = await api_headers(
        app_client, mio, email=await _colaborador(mio, {"agents": "read", "pack_commerce": "none"}), password=PASSWORD
    )
    cuerpo = await _registros(app_client, sin_ventas, mio_ventas)
    assert cuerpo["registros"] == [] and cuerpo["total"] == 0 and cuerpo["lote"] is None
    lee_ventas = await api_headers(
        app_client, mio, email=await _colaborador(mio, {"agents": "read", "pack_commerce": "read"}), password=PASSWORD
    )
    assert (await _registros(app_client, lee_ventas, mio_ventas))["total"] == 3

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", False)
    assert (await app_client.get(f"{_BASE}/{mio_ventas}/datos-de-prueba/registros", headers=h)).status_code == 404


# ---------------------------------------------------------------- suscripciones
async def test_planes_y_suscripciones(app_client, monkeypatch, qa_on) -> None:  # noqa: F811
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.qa.contrato_datos import RegistroSembrado

    slug = uniq("qa16s")
    tid, _ = await onboard_tenant(
        name=f"QA16 {slug}",
        slug=slug,
        business_description="Gimnasio de barrio con planes mensuales y anuales.",
        packs=["pack_commerce", "pack_subscriptions"],
        admin_email="admin@local",
        sembrar_ejemplos=False,
    )
    agente = await _agente(tid, "suscripciones", ["pack_subscriptions.cambiar_plan"])
    h = await api_headers(app_client, tid)
    planes = {
        "planes": [
            {"name": "Plan libre", "price": 0, "periodicidad": "mensual", "trial_dias": 7},
            {"name": "Plan anual", "price": 900000, "periodicidad": "anual"},
        ]
    }
    _proveedor(monkeypatch, ProveedorGuionado(json.dumps(planes)))
    assert (await app_client.post(f"{_BASE}/{agente}/datos-de-prueba", headers=h)).status_code == 201
    cuerpo = await _registros(app_client, h, agente)
    assert _filas(cuerpo) == [
        ("plan", "Plan anual", "$900.000 anual", "llm"),
        ("plan", "Plan libre", "$0 mensual · 7 días de prueba", "llm"),
    ]
    assert cuerpo["sin_detalle"] == 0

    # Las suscripciones las siembra el módulo (sin agente): se prueban con el hook del pack.
    _proveedor(monkeypatch, ProveedorGuionado(error=TimeoutError()))
    r = await app_client.post("/api/v1/admin/datos-de-prueba/suscripciones", headers=h)
    assert r.status_code == 201, r.text
    filas = await _sql(
        tid,
        "select kind, record_ref from test_data_records where tenant_id = :t and modulo = 'suscripciones' "
        "and deleted_at is null",
        t=tid,
    )
    refs = [RegistroSembrado(kind=k, ref=ref) for k, ref in filas]
    pack = plugin_manager.get("pack_subscriptions")
    descritos = await pack.describir_registros(tid, refs)
    subs = [d for d in descritos if d.kind == "suscripcion"]
    assert len(subs) == sum(1 for k, _ in filas if k == "suscripcion") > 0
    assert all(d.nombre and d.nombre != "Cliente" and d.detalle and " · " in d.detalle for d in subs), subs
    assert {d.kind for d in descritos} <= {"plan", "suscripcion"}
    otro = await _negocio()
    assert await pack.describir_registros(otro, refs) == []
