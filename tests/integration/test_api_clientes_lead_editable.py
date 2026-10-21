"""ADR-0149 · Estado e interés del lead de venta editables desde la ficha del cliente.

Con la BD real: la ficha trae los campos editables de pack_commerce con su valor (código, no
etiqueta); `PATCH /admin/leads/{id}/pack/pack_commerce` cambia el lead que muestra la ficha sin
crear otra fila, o lo crea si no había, sin copiar datos de contacto; el core rechaza claves y
valores fuera de lo declarado y un pack sin edición; nada cruza negocios. Las tools de lead
reflejan nombre, correo y teléfono en el perfil (la fuente de los datos de contacto).
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

    slug = uniq("leadedit")
    tid, _ = await onboard_tenant(
        name=f"Lead editable {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    return tid


async def _cliente(tid: uuid.UUID, nombre: str) -> tuple[uuid.UUID, uuid.UUID]:
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
            return cliente.id, conv.id


async def _leads(tid: uuid.UUID, customer_id: uuid.UUID) -> list:
    from pack_commerce.models import Lead
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return list(
                (
                    await session.execute(
                        select(Lead)
                        .where(Lead.tenant_id == tid, Lead.customer_id == customer_id)
                        .order_by(Lead.last_activity_at.desc())
                    )
                ).scalars()
            )


def _editables(ficha: dict) -> dict[str, dict]:
    return {e["key"]: e for e in ficha["editables"] if e["plugin_id"] == "pack_commerce"}


async def test_edita_el_lead_que_muestra_la_ficha_sin_duplicar(app_client) -> None:
    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ana, _ = await _cliente(tid, "Ana")
    hace_un_mes = datetime.now(UTC) - timedelta(days=30)
    hace_una_hora = datetime.now(UTC) - timedelta(hours=1)
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(Lead(tenant_id=tid, customer_id=ana, status="nuevo", interest="viejo", last_activity_at=hace_un_mes))
            session.add(
                Lead(tenant_id=tid, customer_id=ana, status="calificado", interest="plan basic", name="Ana L",
                     phone="300", email="ana@x.co", last_activity_at=hace_una_hora)
            )
    headers = await api_headers(app_client, tid)

    ficha = (await app_client.get(f"{URL}/{ana}", headers=headers)).json()
    editables = _editables(ficha)
    assert editables["estado_lead"]["value"] == "calificado"  # código, no la etiqueta
    assert editables["estado_lead"]["tipo"] == "opciones"
    assert {o["value"] for o in editables["estado_lead"]["options"]} == {
        "nuevo", "calificado", "contactado", "convertido", "perdido"
    }
    assert editables["interes"]["value"] == "plan basic"
    assert editables["interes"]["tipo"] == "texto"
    # La ficha no muestra los datos de contacto del lead (la fuente es el perfil).
    assert not {"name", "phone", "email"} & set(ficha["extras"])

    # Solo el interés: no mueve la actividad (no rearma el seguimiento automático).
    r = await app_client.patch(
        f"{URL}/{ana}/pack/pack_commerce", json={"valores": {"interes": "  plan pro  "}}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert _editables(r.json())["interes"]["value"] == "plan pro"
    leads = await _leads(tid, ana)
    assert len(leads) == 2
    assert leads[0].interest == "plan pro"
    assert leads[0].last_activity_at == hace_una_hora

    r = await app_client.patch(
        f"{URL}/{ana}/pack/pack_commerce", json={"valores": {"estado_lead": "perdido"}}, headers=headers
    )
    assert r.status_code == 200, r.text
    ficha = r.json()
    assert _editables(ficha)["estado_lead"]["value"] == "perdido"
    assert ficha["extras"]["estado_lead"] == "Perdido"
    leads = await _leads(tid, ana)
    assert len(leads) == 2  # editó el más reciente, no creó otro
    assert leads[0].status == "perdido"
    assert leads[0].last_activity_at > hace_una_hora
    assert leads[1].status == "nuevo" and leads[1].interest == "viejo"
    # Los datos de contacto del lead no se tocan.
    assert (leads[0].name, leads[0].phone, leads[0].email) == ("Ana L", "300", "ana@x.co")

    # El filtro de la lista ve el estado nuevo.
    lista = (await app_client.get(URL, params={"filtro": "estado_lead:perdido"}, headers=headers)).json()
    assert str(ana) in {i["id"] for i in lista["items"]}


async def test_crea_el_lead_si_no_habia_sin_datos_de_contacto(app_client) -> None:
    tid = await _negocio()
    beto, _ = await _cliente(tid, "Beto")
    headers = await api_headers(app_client, tid)

    ficha = (await app_client.get(f"{URL}/{beto}", headers=headers)).json()
    assert _editables(ficha)["estado_lead"]["value"] is None

    r = await app_client.patch(
        f"{URL}/{beto}/pack/pack_commerce",
        json={"valores": {"estado_lead": "contactado", "interes": "domicilios"}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    leads = await _leads(tid, beto)
    assert len(leads) == 1
    assert (leads[0].status, leads[0].interest) == ("contactado", "domicilios")
    assert leads[0].idempotency_key == f"manual:{beto}"
    assert (leads[0].name, leads[0].phone, leads[0].email) == (None, None, None)

    # Otra edición usa ese mismo lead.
    r = await app_client.patch(
        f"{URL}/{beto}/pack/pack_commerce", json={"valores": {"estado_lead": "convertido"}}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert [lead.status for lead in await _leads(tid, beto)] == ["convertido"]


async def test_valida_claves_valores_y_pack(app_client) -> None:
    tid = await _negocio()
    caro, _ = await _cliente(tid, "Caro")
    headers = await api_headers(app_client, tid)
    ruta = f"{URL}/{caro}/pack/pack_commerce"

    r = await app_client.patch(ruta, json={"valores": {"estado_lead": "ganado"}}, headers=headers)
    assert r.status_code == 400
    assert "estado_lead" in r.json()["error"]["fields"]

    # `puntaje` es una columna del pack, pero no editable; `name` es del perfil, no del lead.
    r = await app_client.patch(ruta, json={"valores": {"puntaje": "9", "name": "X"}}, headers=headers)
    assert r.status_code == 400
    assert set(r.json()["error"]["fields"]) == {"puntaje", "name"}

    r = await app_client.patch(ruta, json={"valores": {"interes": "x" * 501}}, headers=headers)
    assert r.status_code == 400

    assert (await app_client.patch(ruta, json={"valores": {}}, headers=headers)).status_code == 400

    # Un pack sin edición, uno inexistente y uno no habilitado no se distinguen: 404.
    for plugin in ("pack_calendar", "pack_inexistente", "pack_cobranza"):
        r = await app_client.patch(
            f"{URL}/{caro}/pack/{plugin}", json={"valores": {"estado_lead": "perdido"}}, headers=headers
        )
        assert r.status_code == 404, (plugin, r.text)

    assert await _leads(tid, caro) == []


async def test_no_cruza_negocios(app_client) -> None:
    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    otro = await _negocio()
    ajeno, _ = await _cliente(otro, "Ajeno")
    async with tenant_scope(otro):
        async with get_session() as session:
            session.add(Lead(tenant_id=otro, customer_id=ajeno, status="calificado", interest="suyo"))
    headers = await api_headers(app_client, tid)

    r = await app_client.patch(
        f"{URL}/{ajeno}/pack/pack_commerce", json={"valores": {"estado_lead": "perdido"}}, headers=headers
    )
    assert r.status_code == 404
    leads = await _leads(otro, ajeno)
    assert [(lead.status, lead.interest) for lead in leads] == [("calificado", "suyo")]


async def test_tools_de_lead_reflejan_el_contacto_en_el_perfil() -> None:
    from pack_commerce.models import Lead
    from pack_commerce.skills import LeadSkill
    from sqlalchemy import select

    from core.db.models import MemoryFact
    from core.db.session import get_session
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    dani, conv = await _cliente(tid, "Dani")
    skill = LeadSkill()

    def ctx() -> ToolContext:
        return ToolContext(conversation_id=conv, customer_id=dani, idempotency_key=uniq("idem"), channel="whatsapp")

    async with tenant_scope(tid):
        creado = await skill.crear_lead({"name": "Daniela", "interest": "plan", "phone": "3001112233"}, ctx())
        assert creado["status"] == "created"
        # Un segundo lead más viejo (intake, ficha): las tools siguen con el más reciente, sin fallar.
        async with get_session() as session:
            session.add(
                Lead(tenant_id=tid, customer_id=dani, interest="viejo",
                     last_activity_at=datetime.now(UTC) - timedelta(days=3))
            )
        actualizado = await skill.actualizar_lead({"phone": "3009998877", "name": "Daniela Gómez"}, ctx())
        # La salida de la tool no cambia.
        assert actualizado == {"lead_id": creado["lead_id"], "status": "actualizado"}
        async with get_session() as session:
            perfil = {
                f.key: f.value
                for f in (
                    await session.execute(
                        select(MemoryFact).where(
                            MemoryFact.tenant_id == tid,
                            MemoryFact.customer_id == dani,
                            MemoryFact.namespace == "perfil",
                            MemoryFact.superseded_at.is_(None),
                        )
                    )
                ).scalars()
            }
    assert perfil["phone"] == "3009998877"
    leads = await _leads(tid, dani)
    assert [(lead.interest, lead.phone) for lead in leads] == [("plan", "3009998877"), ("viejo", None)]
    assert perfil["name"] == "Daniela Gómez"
