"""HU-CRM-24 · Sumas de valor por etapa de `GET /admin/leads?facetas=estado_lead&resumen=valor`.

Con la BD real: el tablero de embudo muestra cuánto vale cada columna. `sumas` trae el valor estimado de
los leads de cada etapa, respeta el resto de los filtros pero no el propio (igual que los contadores, así
la columna suma lo mismo que la lista de esa etapa), no cuenta a los clientes sin valor ni sin lead, no
aparece si no se pide y nunca cruza negocios.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

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

    slug = uniq("embudo")
    tid, _ = await onboard_tenant(
        name=f"Embudo {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    return tid


async def _cliente(tid: uuid.UUID, nombre: str, *, etapa: str | None = None, valor: str | None = None) -> uuid.UUID:
    """Un cliente con su conversación y, si se pide, un lead en `etapa` con `valor` estimado."""
    from pack_commerce.models import Lead

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre)
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id)
            session.add(conv)
            await session.flush()
            if etapa:
                session.add(
                    Lead(
                        tenant_id=tid,
                        customer_id=cliente.id,
                        conversation_id=conv.id,
                        status=etapa,
                        estimated_value=Decimal(valor) if valor is not None else None,
                    )
                )
            return cliente.id


def _faceta(lista: dict, clave: str) -> dict:
    return next(f for f in lista["facetas"] if f["key"] == clave)


async def _pedir(app_client, headers, **params):
    r = await app_client.get(URL, params={"facetas": "estado_lead", "resumen": "valor", **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_suma_el_valor_de_cada_etapa(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo", valor="100000")
    await _cliente(tid, "Beto", etapa="nuevo", valor="250000.50")
    await _cliente(tid, "Carla", etapa="calificado", valor="40000")
    await _cliente(tid, "Dani", etapa="nuevo")  # lead sin valor: cuenta en la columna pero no suma
    await _cliente(tid, "Eva")  # sin lead: no está en ninguna columna
    headers = await api_headers(app_client, tid)

    faceta = _faceta(await _pedir(app_client, headers), "estado_lead")
    assert faceta["sumas"] == {
        "nuevo": 350000.5,
        "calificado": 40000.0,
        "contactado": 0.0,
        "convertido": 0.0,
        "perdido": 0.0,
    }
    assert faceta["counts"]["nuevo"] == 3  # Dani cuenta aunque no tenga valor
    assert faceta["sin_valor"] == 1


async def test_sin_pedir_el_resumen_no_hay_sumas(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo", valor="100000")
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"facetas": "estado_lead"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _faceta(r.json(), "estado_lead")["sumas"] is None


async def test_la_suma_de_una_columna_es_la_de_su_lista(app_client) -> None:
    tid = await _negocio()
    for i, valor in enumerate(["1000", "2500.25", None, "300"]):
        await _cliente(tid, f"Cliente {i}", etapa="calificado", valor=valor)
    await _cliente(tid, "Otro", etapa="nuevo", valor="999")
    headers = await api_headers(app_client, tid)

    faceta = _faceta(await _pedir(app_client, headers), "estado_lead")
    lista = (
        await app_client.get(URL, params={"filtro": "estado_lead:calificado", "page_size": 100}, headers=headers)
    ).json()
    suma_de_la_lista = sum(i["extras"].get("valor_estimado") or 0 for i in lista["items"])
    assert lista["total"] == faceta["counts"]["calificado"] == 4
    assert faceta["sumas"]["calificado"] == pytest.approx(suma_de_la_lista) == pytest.approx(3800.25)


async def test_las_sumas_respetan_los_otros_filtros_pero_no_el_propio(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana Gómez", etapa="nuevo", valor="100")
    await _cliente(tid, "Ana Pérez", etapa="calificado", valor="200")
    await _cliente(tid, "Beto Ruiz", etapa="calificado", valor="5000")
    headers = await api_headers(app_client, tid)

    # La búsqueda acota las sumas; la etapa elegida no, para que las demás columnas sigan visibles.
    cuerpo = await _pedir(app_client, headers, q="Ana", filtro="estado_lead:nuevo")
    faceta = _faceta(cuerpo, "estado_lead")
    assert faceta["sumas"]["nuevo"] == 100.0
    assert faceta["sumas"]["calificado"] == 200.0  # Beto queda fuera por la búsqueda


async def test_otro_filtro_de_valor_tambien_acota_las_sumas(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo", valor="50000")
    await _cliente(tid, "Beto", etapa="nuevo", valor="800000")
    headers = await api_headers(app_client, tid)

    faceta = _faceta(await _pedir(app_client, headers, filtro="valor:hasta_100000"), "estado_lead")
    assert faceta["sumas"]["nuevo"] == 50000.0
    assert faceta["counts"]["nuevo"] == 1


async def test_las_sumas_tambien_salen_con_otro_filtro_del_pack(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo", valor="100")
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"facetas": "clasificacion", "resumen": "valor"}, headers=headers)
    assert r.status_code == 200, r.text
    # `clasificacion` también es del pack de comercio, que sabe sumar el valor por cliente.
    faceta = _faceta(r.json(), "clasificacion")
    assert faceta["sumas"] is not None
    assert sum(faceta["sumas"].values()) == 100.0


async def test_un_resumen_desconocido_responde_422(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"facetas": "estado_lead", "resumen": "inventado"}, headers=headers)
    assert r.status_code == 422


async def test_las_sumas_no_cruzan_negocios(app_client) -> None:
    tid = await _negocio()
    otro = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo", valor="100")
    await _cliente(otro, "Ajena", etapa="nuevo", valor="999999")
    headers = await api_headers(app_client, tid)

    faceta = _faceta(await _pedir(app_client, headers), "estado_lead")
    assert faceta["sumas"]["nuevo"] == 100.0
    assert faceta["total"] == 1
