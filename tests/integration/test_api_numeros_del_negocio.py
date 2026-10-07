"""`GET /api/v1/admin/meta/connections/{id}/business-numbers`: números del portafolio de Meta.

Con la BD real y Graph falso: forma de la respuesta, la conexión de otro negocio no llega a
Meta, un colaborador con lectura puede verla y no se escribe nada en `phone_numbers`.
"""

from __future__ import annotations

import os

import pytest
from _api_helpers import api_headers, uniq
from test_api_registrar_numero import _negocio_con_byok

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _graph_falso(monkeypatch, waba_id: str, phone_id: str) -> list[str]:
    import core.gateway.meta_phone_numbers as phone_numbers_mod

    llamadas: list[str] = []
    biz = "biz-" + waba_id

    async def fake_graph_fetch(path, *, method="GET", **_kwargs):
        llamadas.append(path)
        assert method == "GET"
        if path == f"/{waba_id}":
            return {"owner_business_info": {"id": biz, "name": "Portafolio"}}
        if path == f"/{biz}/owned_whatsapp_business_accounts":
            return {"data": [{"id": waba_id, "name": "Mía"}, {"id": "otra-" + waba_id, "name": "Otra"}]}
        if path == f"/{waba_id}/phone_numbers":
            return {"data": [{"id": phone_id, "display_phone_number": "+57 300", "status": "CONNECTED"}]}
        if path == f"/otra-{waba_id}/phone_numbers":
            return {"data": [{"id": "ajeno-" + phone_id, "display_phone_number": "+57 301", "status": "PENDING"}]}
        raise AssertionError(f"path no esperado: {path}")

    monkeypatch.setattr(phone_numbers_mod, "graph_fetch", fake_graph_fetch)
    return llamadas


def _url(cid) -> str:
    return f"/api/v1/admin/meta/connections/{cid}/business-numbers"


async def test_lista_el_portafolio_y_no_guarda_nada(app_client, monkeypatch) -> None:
    from sqlalchemy import func, select

    from core.db.models import PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    _graph_falso(monkeypatch, waba_id, phone_id)

    r = await app_client.get(_url(cid), headers=await api_headers(app_client, tid))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["business_name"] == "Portafolio"
    por_id = {n["phone_number_id"]: n for n in body["numbers"]}
    assert por_id[phone_id]["in_this_connection"] is True
    assert por_id["ajeno-" + phone_id]["in_this_connection"] is False
    assert por_id["ajeno-" + phone_id]["waba_name"] == "Otra"
    async with tenant_scope(tid):
        async with get_session() as session:
            total = (
                await session.execute(select(func.count()).select_from(PhoneNumber).where(PhoneNumber.tenant_id == tid))
            ).scalar_one()
    assert total == 1  # solo el número que ya tenía la conexión


async def test_la_conexion_de_otro_negocio_no_llega_a_meta(app_client, monkeypatch) -> None:
    _tid_a, cid_a, waba_a, phone_a = await _negocio_con_byok()
    tid_b, _cid_b, _waba_b, _phone_b = await _negocio_con_byok()
    llamadas = _graph_falso(monkeypatch, waba_a, phone_a)

    r = await app_client.get(_url(cid_a), headers=await api_headers(app_client, tid_b))

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "business_numbers_failed"
    assert llamadas == []


async def test_un_colaborador_con_lectura_la_ve(app_client, monkeypatch) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    _graph_falso(monkeypatch, waba_id, phone_id)
    email = f"{uniq('lector')}@example.test"
    async with get_session() as session:
        lector = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(lector)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=lector.id, role="colaborador"))
        session.add(AdminPermission(admin_id=lector.id, tenant_id=tid, section="connections", access="read"))

    headers = await api_headers(app_client, tid, email=email, password="clave-segura-123")
    r = await app_client.get(_url(cid), headers=headers)

    assert r.status_code == 200, r.text


async def test_el_numero_que_tiene_otro_negocio_cuenta_pero_no_se_muestra(app_client, monkeypatch) -> None:
    # Token de agencia (BYOK): el portafolio tiene una WABA de otro negocio de la plataforma.
    from core.db.models import PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    tid_otro, cid_otro, _waba_otro, _phone_otro = await _negocio_con_byok()
    async with tenant_scope(tid_otro):
        async with get_session() as session:
            session.add(PhoneNumber(tenant_id=tid_otro, connection_id=cid_otro, phone_number_id="ajeno-" + phone_id))
    _graph_falso(monkeypatch, waba_id, phone_id)

    r = await app_client.get(_url(cid), headers=await api_headers(app_client, tid))

    assert r.status_code == 200, r.text
    body = r.json()
    assert [n["phone_number_id"] for n in body["numbers"]] == [phone_id]
    assert body["other_tenants_count"] == 1
    assert "+57 301" not in r.text
