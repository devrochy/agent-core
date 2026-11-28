"""Clientes (rediseño 2026-11): alta manual e importación de leads desde el panel.

Con la BD real: un lead creado a mano queda con su cliente, perfil y etapa; el mismo teléfono no crea un segundo
lead (409 con su id); el CSV importa las filas buenas, informa las malas y no cruza negocios.
"""

from __future__ import annotations

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
    from _habilidades import onboard_tenant

    slug = uniq("manual")
    tid, _ = await onboard_tenant(
        name=f"Manual {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    return tid


async def test_alta_manual_reutiliza_el_cliente_y_no_duplica_el_lead(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    telefono = f"300{uuid.uuid4().int % 10_000_000:07d}"

    r = await app_client.post(
        URL,
        json={
            "nombre": "Ana Manual",
            "telefono": telefono,
            "email": "ana.manual@correo.com",
            "interes": "Plan mensual",
            "estado": "contactado",
            "valor": 250000,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    creado = r.json()
    assert creado["creado"] is True

    detalle = await app_client.get(f"{URL}/{creado['id']}", headers=headers)
    assert detalle.status_code == 200, detalle.text
    datos = detalle.json()
    assert datos["perfil"]["name"] == "Ana Manual"
    assert datos["perfil"]["email"] == "ana.manual@correo.com"
    assert datos["extras"]["estado_lead"] == "Contactado"
    assert datos["extras"]["origen"] == "Manual"

    repetido = await app_client.post(
        URL,
        json={"nombre": "Otra persona", "telefono": telefono, "interes": "Otra cosa"},
        headers=headers,
    )
    assert repetido.status_code == 409
    assert repetido.json()["error"]["details"]["customer_id"] == creado["id"]


async def test_importa_csv_y_reporta_las_filas_malas(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)
    sufijo = uniq("imp").replace("-", "")
    digitos = str(uuid.uuid4().int)[:7]
    contenido = (
        "nombre,telefono,email,interes,estado\n"
        f"Ana {sufijo},300{digitos},ana.{sufijo}@correo.com,Plan,nuevo\n"
        f"Beto {sufijo},,beto.{sufijo}@correo.com,Asesoría,calificado\n"
        "Sin contacto,,,\n"
    ).encode()

    r = await app_client.post(
        f"{URL}/import",
        files={"archivo": ("leads.csv", contenido, "text/csv")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    resumen = r.json()
    assert resumen["creados"] == 2
    assert resumen["actualizados"] == 0
    assert resumen["omitidos"] == 1
    assert resumen["errores"][0]["fila"] == 4

    lista = await app_client.get(URL, params={"q": f"Beto {sufijo}"}, headers=headers)
    assert lista.status_code == 200
    assert lista.json()["total"] == 1
    assert lista.json()["items"][0]["extras"]["estado_lead"] == "Calificado"
