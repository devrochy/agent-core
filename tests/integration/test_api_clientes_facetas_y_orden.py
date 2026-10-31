"""HU-CRM-17 · Contadores por opción, orden y última actividad de `GET /admin/leads`.

Con la BD real: `facetas=estado_lead` cuenta los clientes por etapa de su último lead con una sola
consulta agrupada, respeta el resto de los filtros pero no el propio (las pestañas de un mismo
filtro suman contra el mismo total) y separa los «sin lead»; una clave sin contadores responde 400;
`orden` ordena por nombre o por el último mensaje y cada fila trae `ultima_actividad`; nada cruza
negocios.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

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

    slug = uniq("facetas")
    tid, _ = await onboard_tenant(
        name=f"Facetas {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    return tid


async def _cliente(
    tid: uuid.UUID,
    nombre: str,
    *,
    etapa: str | None = None,
    ultimo_mensaje: datetime | None = None,
) -> uuid.UUID:
    """Un cliente con su conversación; opcionalmente un lead en `etapa` y un mensaje en `ultimo_mensaje`."""
    from pack_commerce.models import Lead

    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre
            )
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id)
            session.add(conv)
            await session.flush()
            if etapa:
                session.add(
                    Lead(
                        tenant_id=tid, customer_id=cliente.id, conversation_id=conv.id, status=etapa
                    )
                )
            if ultimo_mensaje is not None:
                session.add(
                    Message(
                        tenant_id=tid,
                        conversation_id=conv.id,
                        direction="inbound",
                        sender_type="customer",
                        content="hola",
                        created_at=ultimo_mensaje,
                    )
                )
            return cliente.id


def _faceta(lista: dict, clave: str) -> dict:
    return next(f for f in lista["facetas"] if f["key"] == clave)


async def test_cuenta_por_etapa_y_separa_los_sin_lead(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo")
    await _cliente(tid, "Beto", etapa="calificado")
    await _cliente(tid, "Carla", etapa="calificado")
    await _cliente(tid, "Dani")  # sin lead
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"facetas": "estado_lead"}, headers=headers)
    assert r.status_code == 200, r.text
    faceta = _faceta(r.json(), "estado_lead")
    assert faceta["counts"] == {
        "nuevo": 1,
        "calificado": 2,
        "contactado": 0,
        "convertido": 0,
        "perdido": 0,
    }
    assert faceta["sin_valor"] == 1
    assert faceta["total"] == 4
    assert sum(faceta["counts"].values()) + faceta["sin_valor"] == faceta["total"]


async def test_sin_pedir_facetas_la_respuesta_no_las_calcula(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo")
    headers = await api_headers(app_client, tid)

    lista = (await app_client.get(URL, headers=headers)).json()
    assert lista["facetas"] == []
    assert "estado_lead" in {f["key"] for f in lista["filters"]}


async def test_el_contador_respeta_los_otros_filtros_pero_no_el_propio(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana Gómez", etapa="nuevo")
    await _cliente(tid, "Ana Pérez", etapa="calificado")
    await _cliente(tid, "Beto Ruiz", etapa="calificado")
    headers = await api_headers(app_client, tid)

    # La búsqueda sí acota los contadores; el filtro de etapa elegido no: las demás pestañas siguen visibles.
    r = await app_client.get(
        URL,
        params={"facetas": "estado_lead", "q": "Ana", "filtro": "estado_lead:nuevo"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert [i["cliente"] for i in cuerpo["items"]] == ["Ana Gómez"]
    faceta = _faceta(cuerpo, "estado_lead")
    assert faceta["counts"]["nuevo"] == 1
    assert faceta["counts"]["calificado"] == 1  # Ana Pérez; Beto queda fuera por la búsqueda
    assert faceta["total"] == 2


async def test_clasificacion_cuenta_a_los_clientes_sin_pedidos_como_nuevos(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana")
    await _cliente(tid, "Beto")
    headers = await api_headers(app_client, tid)

    faceta = _faceta(
        (await app_client.get(URL, params={"facetas": "clasificacion"}, headers=headers)).json(),
        "clasificacion",
    )
    assert faceta["counts"]["nuevo"] == 2
    assert sum(faceta["counts"].values()) + faceta["sin_valor"] == faceta["total"] == 2


async def test_pide_dos_facetas_a_la_vez(app_client) -> None:
    tid = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo")
    headers = await api_headers(app_client, tid)

    r = await app_client.get(
        URL, params=[("facetas", "estado_lead"), ("facetas", "clasificacion")], headers=headers
    )
    assert r.status_code == 200, r.text
    assert [f["key"] for f in r.json()["facetas"]] == ["estado_lead", "clasificacion"]


async def test_una_clave_sin_contadores_responde_400(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"facetas": "inventada"}, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "filtro_invalido"


async def test_los_contadores_no_cruzan_negocios(app_client) -> None:
    tid = await _negocio()
    otro = await _negocio()
    await _cliente(tid, "Ana", etapa="nuevo")
    await _cliente(otro, "Ajena 1", etapa="nuevo")
    await _cliente(otro, "Ajena 2", etapa="perdido")
    headers = await api_headers(app_client, tid)

    faceta = _faceta(
        (await app_client.get(URL, params={"facetas": "estado_lead"}, headers=headers)).json(),
        "estado_lead",
    )
    assert faceta["counts"]["nuevo"] == 1
    assert faceta["counts"]["perdido"] == 0
    assert faceta["total"] == 1


async def test_ordena_por_nombre_en_los_dos_sentidos(app_client) -> None:
    tid = await _negocio()
    for nombre in ("Carla", "ana", "Beto"):
        await _cliente(tid, nombre)
    headers = await api_headers(app_client, tid)

    asc = [
        i["cliente"]
        for i in (await app_client.get(URL, params={"orden": "nombre"}, headers=headers)).json()[
            "items"
        ]
    ]
    desc = [
        i["cliente"]
        for i in (
            await app_client.get(URL, params={"orden": "nombre_desc"}, headers=headers)
        ).json()["items"]
    ]
    assert asc == ["ana", "Beto", "Carla"]  # sin distinguir mayúsculas
    assert desc == ["Carla", "Beto", "ana"]


async def test_ordena_por_ultima_actividad_y_deja_al_final_a_quien_no_escribio(app_client) -> None:
    tid = await _negocio()
    ahora = datetime.now(UTC)
    vieja = await _cliente(tid, "Vieja", ultimo_mensaje=ahora - timedelta(days=5))
    reciente = await _cliente(tid, "Reciente", ultimo_mensaje=ahora - timedelta(minutes=3))
    muda = await _cliente(tid, "Muda")
    headers = await api_headers(app_client, tid)

    items = (await app_client.get(URL, params={"orden": "actividad"}, headers=headers)).json()[
        "items"
    ]
    assert [i["id"] for i in items] == [str(reciente), str(vieja), str(muda)]
    por_id = {i["id"]: i for i in items}
    assert por_id[str(muda)]["ultima_actividad"] is None
    assert datetime.fromisoformat(
        por_id[str(reciente)]["ultima_actividad"]
    ) > datetime.fromisoformat(por_id[str(vieja)]["ultima_actividad"])


async def test_la_actividad_es_el_ultimo_mensaje_de_cualquiera_de_sus_conversaciones(
    app_client,
) -> None:
    from core.db.models import Conversation, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ahora = datetime.now(UTC)
    cliente = await _cliente(tid, "Dos chats", ultimo_mensaje=ahora - timedelta(days=2))
    async with tenant_scope(tid):
        async with get_session() as session:
            segunda = Conversation(tenant_id=tid, customer_id=cliente)
            session.add(segunda)
            await session.flush()
            session.add(
                Message(
                    tenant_id=tid,
                    conversation_id=segunda.id,
                    direction="inbound",
                    sender_type="customer",
                    content="otra vez",
                    created_at=ahora - timedelta(minutes=1),
                )
            )
    headers = await api_headers(app_client, tid)

    fila = (await app_client.get(URL, headers=headers)).json()["items"][0]
    assert (
        abs(
            (
                datetime.fromisoformat(fila["ultima_actividad"]) - (ahora - timedelta(minutes=1))
            ).total_seconds()
        )
        < 2
    )


async def test_un_orden_desconocido_responde_422(app_client) -> None:
    tid = await _negocio()
    headers = await api_headers(app_client, tid)

    r = await app_client.get(URL, params={"orden": "por-el-color"}, headers=headers)
    assert r.status_code == 422


async def test_el_orden_por_defecto_no_cambia(app_client) -> None:
    tid = await _negocio()
    primero = await _cliente(tid, "Primero")
    segundo = await _cliente(tid, "Segundo")
    headers = await api_headers(app_client, tid)

    items = (await app_client.get(URL, headers=headers)).json()["items"]
    assert [i["id"] for i in items] == [
        str(segundo),
        str(primero),
    ]  # el más nuevo primero, como hasta hoy
