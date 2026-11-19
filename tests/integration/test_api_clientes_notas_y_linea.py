"""HU-CRM-04 · notas del cliente y línea de tiempo (ADR-0147).

Con la BD real: las notas se crean, se listan y se borran (solo autor u owner/master); la purga de
memoria las olvida; el agente las recibe en su bloque; la línea de tiempo mezcla core y packs,
pagina sin perder ni repetir eventos (también con empates de hora) y no cruza negocios.
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
PASSWORD = "Colab-Notas-2026!"


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("notas")
    tid, _ = await onboard_tenant(
        name=f"Notas {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce", "pack_calendar"], sembrar_ejemplos=False,
    )
    return tid


async def _cliente(tid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            c = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Ana")
            session.add(c)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=c.id, started_at=datetime.now(UTC) - timedelta(days=3))
            session.add(conv)
            await session.flush()
            return c.id, conv.id


async def _colaborador(tid: uuid.UUID) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        session.add(AdminPermission(admin_id=admin.id, section="leads", access="write", tenant_id=tid))
    return email


async def test_notas_crear_listar_borrar_y_permisos(app_client) -> None:
    tid = await _negocio()
    ana, _ = await _cliente(tid)
    master = await api_headers(app_client, tid)
    colab = await api_headers(app_client, tid, email=await _colaborador(tid), password=PASSWORD)

    r = await app_client.post(f"{URL}/{ana}/notes", json={"text": "  prefiere entregas en la tarde  "}, headers=master)
    assert r.status_code == 201, r.text
    del_master = r.json()
    assert del_master["text"] == "prefiere entregas en la tarde"
    assert del_master["author"] == "Soporte de la plataforma"  # el master no expone su correo
    r = await app_client.post(f"{URL}/{ana}/notes", json={"text": "no llamar antes de las 10"}, headers=colab)
    assert r.status_code == 201, r.text
    del_colab = r.json()

    assert (await app_client.post(f"{URL}/{ana}/notes", json={"text": "   "}, headers=master)).status_code == 422
    assert (await app_client.post(f"{URL}/{ana}/notes", json={"text": "x" * 2001}, headers=master)).status_code == 422

    vista_colab = (await app_client.get(f"{URL}/{ana}/notes", headers=colab)).json()
    assert [n["text"] for n in vista_colab] == ["no llamar antes de las 10", "prefiere entregas en la tarde"]
    assert {n["id"]: n["puede_borrar"] for n in vista_colab} == {del_colab["id"]: True, del_master["id"]: False}

    # El colaborador no borra la nota de otro; el master borra cualquiera.
    assert (await app_client.delete(f"{URL}/{ana}/notes/{del_master['id']}", headers=colab)).status_code == 403
    assert (await app_client.delete(f"{URL}/{ana}/notes/{del_colab['id']}", headers=master)).status_code == 204
    assert (await app_client.delete(f"{URL}/{ana}/notes/{del_colab['id']}", headers=master)).status_code == 404
    assert [n["text"] for n in (await app_client.get(f"{URL}/{ana}/notes", headers=master)).json()] == [
        "prefiere entregas en la tarde"
    ]


async def test_el_agente_recibe_las_notas_y_la_purga_las_olvida(app_client) -> None:
    from core.db.session import get_session
    from core.memory import facts, notas
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ana, _ = await _cliente(tid)
    headers = await api_headers(app_client, tid)
    async with tenant_scope(tid):
        assert await notas.para_prompt(tid, ana) == ""
    await app_client.post(f"{URL}/{ana}/notes", json={"text": "no llamar antes de las 10"}, headers=headers)
    async with tenant_scope(tid):
        bloque = await notas.para_prompt(tid, ana)
    assert '"no llamar antes de las 10"' in bloque

    async with tenant_scope(tid):
        async with get_session() as session:
            await facts.purgar_de_cliente(session, tenant_id=tid, customer_id=ana)
        assert await notas.para_prompt(tid, ana) == ""
    assert (await app_client.get(f"{URL}/{ana}/notes", headers=headers)).json() == []


async def test_linea_de_tiempo_mezcla_core_y_packs(app_client) -> None:
    from pack_calendar.models import Appointment
    from pack_commerce.models import Order
    from sqlalchemy import text

    from core.db.models import Escalation
    from core.db.session import get_session
    from core.memory import facts
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    otro = await _negocio()
    ana, conv = await _cliente(tid)
    ajeno, _ = await _cliente(otro)
    ahora = datetime.now(UTC)
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(Escalation(tenant_id=tid, conversation_id=conv, reason="tool:queja", summary="Llegó tarde"))
            seq = (await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": tid})).scalar()
            session.add(Order(tenant_id=tid, conversation_id=conv, customer_id=ana, idempotency_key=uniq("p"),
                              order_seq=seq, public_order_number=f"OC-{seq:04d}", total=85000,
                              created_at=ahora - timedelta(days=2), status="despachado",
                              status_changed_at=ahora - timedelta(days=1)))
            session.add(Appointment(tenant_id=tid, customer_id=ana, conversation_id=conv, title="Asesoría",
                                    start_at=ahora + timedelta(days=1), end_at=ahora + timedelta(days=1, hours=1)))
            await facts.escribir_hechos(session, tenant_id=tid, customer_id=ana, namespace="perfil",
                                        valores={"ciudad": "Bello"}, source="tool")
        async with get_session() as session:
            await facts.escribir_hechos(session, tenant_id=tid, customer_id=ana, namespace="perfil",
                                        valores={"ciudad": "Medellín"}, source="human")
    headers = await api_headers(app_client, tid)
    await app_client.post(f"{URL}/{ana}/notes", json={"text": "cliente VIP"}, headers=headers)

    r = await app_client.get(f"{URL}/{ana}/timeline", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["partial"] is False and body["next_cursor"] is None
    tipos = [i["kind"] for i in body["items"]]
    assert {"conversacion", "escalacion", "pedido", "cita", "dato", "nota"} <= set(tipos)
    fechas = [i["at"] for i in body["items"]]
    assert fechas == sorted(fechas, reverse=True)
    datos = [i for i in body["items"] if i["kind"] == "dato"]
    assert len(datos) == 2  # el cambio de ciudad se ve con sus dos orígenes
    assert any("una persona" in d["title"] for d in datos) and any("el asistente" in d["title"] for d in datos)
    pedido = next(i for i in body["items"] if i["title"].startswith("Pedido OC-") and "despachado" in i["title"])
    assert pedido["link_kind"] == "order"

    # Aislamiento: el cliente de otro negocio no existe aquí.
    assert (await app_client.get(f"{URL}/{ajeno}/timeline", headers=headers)).status_code == 404
    assert (await app_client.get(f"{URL}/{ana}/timeline", params={"cursor": "basura"}, headers=headers)).status_code == 400


async def test_paginacion_sin_perdidas_ni_repetidos_con_empates(app_client) -> None:
    from core.db.models import CustomerNote
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ana, _ = await _cliente(tid)
    mismo_instante = datetime(2026, 10, 1, 12, tzinfo=UTC)
    async with tenant_scope(tid):
        async with get_session() as session:
            for i in range(7):  # 7 notas en el mismo instante: empates puros
                session.add(CustomerNote(tenant_id=tid, customer_id=ana, text=f"n{i}", created_at=mismo_instante))
    headers = await api_headers(app_client, tid)

    vistos: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = (await app_client.get(f"{URL}/{ana}/timeline", params=params, headers=headers)).json()
        vistos += [f"{i['kind']}|{i['at']}|{i['detail']}|{i['title']}" for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(vistos) == 8  # 7 notas + la conversación
    assert len(set(vistos)) == 8
