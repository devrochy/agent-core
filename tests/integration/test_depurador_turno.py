"""HU-AOS-28 · depurador del turno (ADR-0169): `GET /admin/turns/{id}` y `turn_id` en los mensajes.

Sin LLM: el turno se siembra como lo escribe el kernel (`core_decisions`, `turn_outcomes`, `audit_log`,
`messages`, `usage_ledger`).
- con `observability` (dueño, master o grant explícito) se ven entrada y salida de las acciones, con
  los datos personales enmascarados; un colaborador sin la fila no la hereda (`detalle_oculto`);
- el dinero solo con `costs`; sin `conversations`, 403; un turno de otro negocio, 404;
- responde en menos de 2 s.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from test_canary import _conversacion_unica
from test_releases_por_conexion import _negocio, _sql

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "Clave-de-prueba-28"


async def _colaborador(tid: uuid.UUID, permisos: dict[str, str]) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        for seccion, acceso in permisos.items():
            session.add(AdminPermission(admin_id=admin.id, tenant_id=tid, section=seccion, access=acceso))
    return email


async def _turno() -> dict[str, Any]:
    tid, _slug, cid = await _negocio()
    conv = await _conversacion_unica(tid, cid)
    turno = uuid.uuid4()
    await _sql(
        tid,
        "insert into core_decisions (id, tenant_id, connection_id, conversation_id, release_lane, outcome, route_source, "
        "candidates, confidence, latency_ms) values (:id, :t, :c, :conv, 'legacy', 'routed', 'rule', "
        "cast(:cand as jsonb), 0.9, 40)",
        id=turno, t=tid, c=cid, conv=conv, cand='[{"procedure_id": "pedido", "owner": "ventas", "score": 0.9}]',
    )
    await _sql(
        tid,
        "insert into turn_outcomes (turn_id, tenant_id, status, verifier, verifier_retries, latency_ms) "
        "values (:id, :t, 'replied', cast(:v as jsonb), 1, 900)",
        id=turno, t=tid, v='[{"kind": "horarios", "verdict": "fail", "count": 1}]',
    )
    await _sql(
        tid,
        "insert into audit_log (id, tenant_id, conversation_id, actor, action, turn_id, input, output) values "
        "(:id, :t, :conv, 'tool:crear_pedido', 'execute', :turno, cast(:i as jsonb), cast(:o as jsonb))",
        id=uuid.uuid4(), t=tid, conv=conv, turno=turno,
        i='{"email": "ana.perez@correo.co", "telefono": "3001234567", "total": 185000}',
        o='{"order_id": "PED-2041", "status": "confirmed"}',
    )
    await _sql(
        tid,
        "insert into audit_log (id, tenant_id, conversation_id, actor, action, turn_id, input, output) values "
        "(:id, :t, :conv, 'core:comando', 'actualizar_tarea', :turno, cast(:i as jsonb), cast(:o as jsonb))",
        id=uuid.uuid4(), t=tid, conv=conv, turno=turno, i='{"keys": ["direccion"]}', o='{"ok": true, "errors": 0}',
    )
    mensaje = uuid.uuid4()
    await _sql(
        tid,
        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content, status, turn_id) "
        "values (:id, :t, :conv, 'outbound', 'agent', 'Listo, tu pedido PED-2041 quedó creado.', 'sent', :turno)",
        id=mensaje, t=tid, conv=conv, turno=turno,
    )
    await _sql(
        tid,
        "insert into usage_ledger (id, tenant_id, conversation_id, model, input_tokens, output_tokens, cost_usd, kind, turn_id, "
        "connection_id) values (:id, :t, :conv, 'fake', 1200, 80, 0.0042, 'turn', :turno, :c)",
        id=uuid.uuid4(), t=tid, conv=conv, turno=turno, c=cid,
    )
    return {"tid": tid, "cid": cid, "conv": conv, "turno": turno, "mensaje": mensaje}


def _accion(detalle: dict[str, Any]) -> dict[str, Any]:
    return next(p for p in detalle["steps"] if p["kind"] == "accion")


async def test_el_dueno_ve_el_turno_completo_y_la_pii_enmascarada(app_client) -> None:
    e = await _turno()
    h = await api_headers(app_client, e["tid"])
    inicio = time.perf_counter()
    r = await app_client.get(f"/api/v1/admin/turns/{e['turno']}", headers=h)
    assert time.perf_counter() - inicio < 2.0
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["decision"]["outcome"] == "routed" and d["decision"]["candidates"][0]["procedure_id"] == "pedido"
    assert d["status"] == "replied" and d["verifier"][0]["kind"] == "horarios" and d["verifier_retries"] == 1
    assert d["can_see_detail"] is True and d["can_see_costs"] is True
    accion = _accion(d)
    assert accion["name"] == "crear_pedido" and accion["action"] == "execute"
    assert accion["input"] == {"email": "a***@correo.co", "telefono": "•••4567", "total": 185000}
    assert accion["output"] == {"order_id": "PED-2041", "status": "confirmed"}
    assert accion["ok"] is True and accion["error_codes"] == []
    assert [p["kind"] for p in d["steps"]].count("comando") == 1
    assert d["delivery"][0]["text"].startswith("Listo") and d["cost"][0]["cost_usd"] == pytest.approx(0.0042)
    # El mensaje del agente trae su turno (abre el depurador desde el chat).
    msgs = (await app_client.get(f"/api/v1/admin/conversations/{e['conv']}", headers=h)).json()["messages"]
    assert [m["turn_id"] for m in msgs if m["id"] == str(e["mensaje"])] == [str(e["turno"])]


async def test_el_colaborador_no_hereda_observabilidad_ni_ve_dinero_sin_costs(app_client) -> None:
    e = await _turno()
    base = f"/api/v1/admin/turns/{e['turno']}"
    sin_fila = await api_headers(app_client, e["tid"], email=await _colaborador(e["tid"], {"costs": "none"}), password=PASSWORD)
    d = (await app_client.get(base, headers=sin_fila)).json()
    accion = _accion(d)
    assert d["can_see_detail"] is False and accion["input"] is None and accion["output"] is None
    assert accion["detalle_oculto"] is True and accion["ok"] is True  # el resultado, sin valores
    assert d["can_see_costs"] is False and d["cost"][0]["cost_usd"] is None
    # Con el permiso explícito ve el detalle, también enmascarado.
    con_grant = await api_headers(
        app_client, e["tid"], email=await _colaborador(e["tid"], {"observability": "read"}), password=PASSWORD
    )
    d = (await app_client.get(base, headers=con_grant)).json()
    assert _accion(d)["input"]["email"] == "a***@correo.co" and d["can_see_detail"] is True


async def test_aislamiento_y_permiso_de_conversaciones(app_client) -> None:
    e = await _turno()
    otro, _s, _c = await _negocio()
    ho = await api_headers(app_client, otro)
    r = await app_client.get(f"/api/v1/admin/turns/{e['turno']}", headers=ho)
    assert r.status_code == 404 and str(e["turno"]) not in r.text
    sin_conv = await api_headers(
        app_client, e["tid"], email=await _colaborador(e["tid"], {"conversations": "none"}), password=PASSWORD
    )
    assert (await app_client.get(f"/api/v1/admin/turns/{e['turno']}", headers=sin_conv)).status_code == 403


async def test_un_turno_del_playground_no_se_depura_aqui(app_client) -> None:
    from core.shared.test_mode import TEST_CHANNEL

    e = await _turno()
    await _sql(e["tid"], "update conversations set channel = :canal where id = :c", canal=TEST_CHANNEL, c=e["conv"])
    h = await api_headers(app_client, e["tid"])
    assert (await app_client.get(f"/api/v1/admin/turns/{e['turno']}", headers=h)).status_code == 404
