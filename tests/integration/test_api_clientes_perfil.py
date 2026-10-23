"""Tanda 1A · Clientes: perfil base, agentes y CSV.

Con la BD real: la lista y la ficha traen el perfil canónico (nombre, alias, teléfono en
E.164, clasificación, ubicación y agentes); `PATCH /admin/leads/{id}/perfil` valida y escribe
por `core/memory/facts.py` con `source="human"`; el CSV incluye los datos base; nada cruza
negocios.
"""

from __future__ import annotations

import csv
import io
import os
import uuid

import pytest
from _api_helpers import api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/leads"


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("perfil")
    tid, _ = await onboard_tenant(
        name=f"Perfil {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    return tid


async def _cliente(tid: uuid.UUID, nombre: str = "Ana") -> uuid.UUID:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.memory import facts
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tid,
                channel="whatsapp",
                external_id=f"whatsapp:{uniq('n')}",
                name=nombre,
                contact_phone="+573001234567",
            )
            session.add(cliente)
            await session.flush()
            await facts.escribir_hechos(
                session,
                tenant_id=tid,
                customer_id=cliente.id,
                namespace=facts.PERFIL,
                valores={
                    "full_name": "Ana María",
                    "alias": "Anita",
                    "cedula": "123456",
                    "city": "Cali",
                },
                source="import",
            )
            return cliente.id


async def test_lista_y_ficha_traen_el_perfil_y_los_agentes(app_client) -> None:
    tid = await _negocio()
    ana = await _cliente(tid)
    headers = await api_headers(app_client, tid)

    lista = (await app_client.get(URL, headers=headers)).json()
    fila = next(i for i in lista["items"] if i["id"] == str(ana))
    assert fila["cliente"] == "Ana María"
    assert fila["perfil"]["name"] == "Ana María"
    assert fila["perfil"]["alias"] == "Anita"
    assert fila["perfil"]["phone_e164"] == "+573001234567"
    assert fila["perfil"]["phone_indicativo"] == "+57"
    assert fila["perfil"]["pais_telefono"] == "CO"
    assert fila["perfil"]["municipio"] == "Cali"
    assert fila["perfil"]["id_document"] == "123456"
    assert {"general", "ventas"} <= set(fila["agentes"])
    assert {"general", "ventas"} <= {a["key"] for a in lista["agents"]}

    ficha = (await app_client.get(f"{URL}/{ana}", headers=headers)).json()
    assert ficha["perfil"]["nombre_visible"] == "Ana María"
    assert "clasificacion" in {c["key"] for c in ficha["campos_perfil"]}
    ventas = next(a for a in ficha["agentes"] if a["key"] == "ventas")
    assert "clasificacion" in {c["key"] for c in ventas["columns"]}


async def test_patch_perfil_valida_y_escribe_como_persona(app_client) -> None:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ana = await _cliente(tid)
    headers = await api_headers(app_client, tid)

    r = await app_client.patch(
        f"{URL}/{ana}/perfil",
        headers=headers,
        json={
            "valores": {
                "name": "Ana María",
                "apellido": "Pérez",
                "phone": "300 123 4567",
                "email": "ana@example.co",
                "tipo_documento": "CC",
                "id_document": "123456",
                "address": "Calle 1 # 2-3",
                "municipio": "Cali",
                "departamento": "Valle del Cauca",
                "pais": "Colombia",
                "clasificacion": "Cliente",
                "latitud": 3.4516,
                "longitud": -76.532,
            }
        },
    )
    assert r.status_code == 200, r.text
    ficha = r.json()
    assert ficha["perfil"]["apellido"] == "Pérez"
    assert ficha["perfil"]["nombre_visible"] == "Ana María Pérez"
    assert ficha["perfil"]["phone_e164"] == "+573001234567"
    assert ficha["perfil"]["tipo_documento"] == "cc"
    assert ficha["perfil"]["clasificacion"] == "cliente"
    assert ficha["perfil"]["clasificacion_label"] == "Cliente"
    assert ficha["perfil"]["mapa_url"].startswith("https://www.openstreetmap.org/?mlat=3.4516")

    datos = {d["key"]: d for d in ficha["datos"]}
    assert datos["apellido"]["source"] == "human"
    assert datos["address"]["source"] == "human"

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = await session.get(Customer, ana)
            assert cliente.attributes["apellido"] == "Pérez"
            assert cliente.attributes["address"] == "Calle 1 # 2-3"
            assert cliente.name == "Ana María"

    for valores, campo in [
        ({"name": "  "}, "name"),
        ({"email": "no-es-correo"}, "email"),
        ({"clasificacion": "inventada"}, "clasificacion"),
        ({"latitud": 100, "longitud": 1}, "latitud"),
        ({"tarjeta": "4111"}, "tarjeta"),
    ]:
        r = await app_client.patch(f"{URL}/{ana}/perfil", headers=headers, json={"valores": valores})
        assert r.status_code == 400, (campo, r.text)
        assert campo in r.json()["error"]["fields"]

    # Un dato canónico no puede colarse por `otros` sin validación.
    r = await app_client.patch(
        f"{URL}/{ana}/perfil", headers=headers, json={"otros": {"clasificacion": "inventada"}}
    )
    assert r.status_code == 400, r.text
    assert "clasificacion" in r.json()["error"]["fields"]


async def test_csv_incluye_datos_base_y_agentes(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid)
    headers = await api_headers(app_client, tid)

    r = await app_client.get(f"{URL}/export.csv", headers=headers)
    assert r.status_code == 200, r.text
    filas = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    cabecera = filas[0]
    assert cabecera[:5] == ["Cliente", "Nombre", "Apellido", "Alias", "Canal"]
    assert "Clasificación" in cabecera and "Agentes" in cabecera and "Teléfono" in cabecera
    fila = next(f for f in filas[1:] if f[0] == "Ana María")
    indice = {nombre: i for i, nombre in enumerate(cabecera)}
    assert fila[indice["Alias"]] == "Anita"
    assert fila[indice["Teléfono"]] == "'+573001234567"
    assert "General" in fila[indice["Agentes"]] and "Ventas" in fila[indice["Agentes"]]


async def test_no_cruza_negocios(app_client) -> None:
    tid = await _negocio()
    otro = await _negocio()
    ajeno = await _cliente(otro, "Ajeno")
    headers = await api_headers(app_client, tid)

    assert (await app_client.get(f"{URL}/{ajeno}", headers=headers)).status_code == 404
    r = await app_client.patch(f"{URL}/{ajeno}/perfil", headers=headers, json={"valores": {"name": "X"}})
    assert r.status_code == 404


async def test_geocodifica_y_guarda_coordenadas(app_client, monkeypatch) -> None:
    from core.api.routers import leads as leads_router
    from core.geocoding import ResultadoGeocodificacion

    tid = await _negocio()
    ana = await _cliente(tid)
    headers = await api_headers(app_client, tid)

    async def _noop_cuota(_tid):
        return None

    async def _geocodificar(_direccion, **_kwargs):
        return ResultadoGeocodificacion(
            latitud=3.4516,
            longitud=-76.532,
            direccion_normalizada="Calle 1, Cali, Valle del Cauca, Colombia",
            precision="exacta",
            proveedor="locationiq",
        )

    monkeypatch.setattr(leads_router, "geocoding_disponible", lambda: True)
    monkeypatch.setattr(leads_router, "consumir_cuota_geocoding", _noop_cuota)
    monkeypatch.setattr(leads_router, "geocodificar_direccion", _geocodificar)

    r = await app_client.post(f"{URL}/{ana}/geocodificar", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["latitud"] == 3.4516
    assert body["precision"] == "exacta"
    assert body["perfil"]["mapa_url"].startswith("https://www.openstreetmap.org/?mlat=3.4516")
    ficha = (await app_client.get(f"{URL}/{ana}", headers=headers)).json()
    por_clave = {d["key"]: d for d in ficha["datos"]}
    assert por_clave["latitud"]["value"] == 3.4516
    assert por_clave["latitud"]["source"] == "human"


async def test_geocodificar_sin_direccion_es_400(app_client, monkeypatch) -> None:
    from core.api.routers import leads as leads_router
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Sin dirección"
            )
            session.add(cliente)
            await session.flush()
            ana = cliente.id
    headers = await api_headers(app_client, tid)
    monkeypatch.setattr(leads_router, "geocoding_disponible", lambda: True)

    r = await app_client.post(f"{URL}/{ana}/geocodificar", headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "geocoding_direccion_vacia"


async def test_geocodificar_no_cruza_negocios(app_client, monkeypatch) -> None:
    from core.api.routers import leads as leads_router

    tid = await _negocio()
    otro = await _negocio()
    ajeno = await _cliente(otro, "Ajeno")
    headers = await api_headers(app_client, tid)
    monkeypatch.setattr(leads_router, "geocoding_disponible", lambda: True)

    assert (await app_client.post(f"{URL}/{ajeno}/geocodificar", headers=headers)).status_code == 404
