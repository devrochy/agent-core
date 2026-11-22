"""HU-CRM-28: la política de datos del cliente es de cada agente especialista."""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "Colab-Crm28-2026!"


def _url(agent_id) -> str:
    return f"/api/v1/admin/agents/{agent_id}/datos-cliente"


async def _negocio(packs: list[str] | None = None):
    from _habilidades import onboard_tenant

    slug = uniq("crm28")
    tid, _ = await onboard_tenant(
        name=f"N {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=packs or ["pack_commerce", "pack_calendar"],
        sembrar_ejemplos=False,
    )
    await _crear_agentes(tid)
    return tid


async def _crear_agentes(tid) -> None:
    """Crea los especialistas de Ventas y Agenda con la tool que dispara su política de datos."""
    from core.db.models import AgentDraftRecord, AgentRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        for key, tool in (("ventas", "crear_pedido"), ("agenda", "create_calendar_event")):
            agente = AgentRecord(id=uuid.uuid4(), tenant_id=tid, key=key, kind="specialist", name=key.title())
            session.add(agente)
            await session.flush()
            session.add(
                AgentDraftRecord(
                    tenant_id=tid,
                    agent_id=agente.id,
                    content={"tools": [{"tool": tool, "enabled": True}]},
                )
            )


async def _agente(tid, key: str = "ventas") -> uuid.UUID:
    from core.db.models import AgentRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        fila = (
            await session.execute(
                select(AgentRecord).where(
                    AgentRecord.tenant_id == tid,
                    AgentRecord.key == key,
                    AgentRecord.kind == "specialist",
                    AgentRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    assert fila is not None, f"no hay especialista {key}"
    return fila.id


async def _colaborador(tid, access: str) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        session.add(AdminPermission(admin_id=admin.id, section="settings", access=access, tenant_id=tid))
    return email


def _campo(cuerpo: dict, accion: str, clave: str) -> dict:
    a = next(x for x in cuerpo["acciones"] if x["id"] == accion)
    return next(c for c in a["campos"] if c["clave"] == clave)


async def test_get_lista_solo_las_operaciones_del_agente(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    ventas = await _agente(tid, "ventas")
    agenda = await _agente(tid, "agenda")

    r = await app_client.get(_url(ventas), headers=h)
    assert r.status_code == 200, r.text
    ids = {a["id"] for a in r.json()["acciones"]}
    assert {"ventas.pedido_domicilio", "ventas.pedido_retiro"} <= ids
    assert "agenda.agendar_cita" not in ids
    doc = _campo(r.json(), "ventas.pedido_domicilio", "id_document")
    assert doc["nivel"] == "requerido" and doc["ajustado"] is False

    r = await app_client.get(_url(agenda), headers=h)
    ids = {a["id"] for a in r.json()["acciones"]}
    assert ids == {"agenda.agendar_cita"}
    assert _campo(r.json(), "agenda.agendar_cita", "email")["ajustable"] is False


async def test_put_guarda_solo_lo_que_difiere_audita_y_afecta_la_lectura(app_client) -> None:
    from core.db.models import AgentRecord, AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    agent_id = await _agente(tid)
    cuerpo = {"politica": {"ventas.pedido_domicilio": {"id_document": "no_pedir", "name": "requerido"}}}
    r = await app_client.put(_url(agent_id), headers=h, json=cuerpo)
    assert r.status_code == 200, r.text
    salida = r.json()
    doc = _campo(salida, "ventas.pedido_domicilio", "id_document")
    assert doc["nivel"] == "no_pedir" and doc["ajustado"] is True and doc["por_defecto"] == "requerido"
    assert _campo(salida, "ventas.pedido_domicilio", "name")["ajustado"] is False  # igual al módulo: no se guarda

    async with tenant_scope(tid), get_session() as s:
        filas = list(
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tid, AuditLog.action == "agent.datos_cliente"
                    )
                )
            ).scalars()
        )
        agente = (
            await s.execute(select(AgentRecord).where(AgentRecord.id == agent_id))
        ).scalar_one()
    assert len(filas) == 1
    assert filas[0].input["despues"]["politica"] == {"ventas.pedido_domicilio": {"id_document": "no_pedir"}}
    assert agente.data_policy["politica"] == {"ventas.pedido_domicilio": {"id_document": "no_pedir"}}

    # Volver a mandar lo mismo no deja otra fila de auditoría; vacío restablece.
    await app_client.put(_url(agent_id), headers=h, json=cuerpo)
    r = await app_client.put(_url(agent_id), headers=h, json={"politica": {}})
    assert _campo(r.json(), "ventas.pedido_domicilio", "id_document")["nivel"] == "requerido"


async def test_put_rechaza_acciones_datos_y_pisos_que_no_se_pueden_ajustar(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    ventas = await _agente(tid, "ventas")
    agenda = await _agente(tid, "agenda")
    for agent_id, politica in (
        (ventas, {"agenda.agendar_cita": {"name": "no_pedir"}}),  # no es operación de este agente
        (ventas, {"ventas.pedido_domicilio": {"inventado": "no_pedir"}}),
        (agenda, {"agenda.agendar_cita": {"email": "no_pedir"}}),  # piso técnico
        (ventas, {"ventas.pedido_domicilio": {"name": "obligatorio"}}),  # nivel inválido
    ):
        r = await app_client.put(_url(agent_id), headers=h, json={"politica": politica})
        assert r.status_code == 422, (politica, r.text)


async def test_solo_quien_puede_escribir_en_configuracion_guarda(app_client) -> None:
    tid = await _negocio()
    agent_id = await _agente(tid)
    lector = await _colaborador(tid, "read")
    h = await api_headers(app_client, tid, email=lector, password=PASSWORD)
    assert (await app_client.get(_url(agent_id), headers=h)).status_code == 200
    r = await app_client.put(_url(agent_id), headers=h, json={"politica": {}})
    assert r.status_code == 403


async def test_los_metodos_de_entrega_se_ajustan_por_agente(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    agent_id = await _agente(tid)
    assert (await app_client.get(_url(agent_id), headers=h)).json()["metodos_entrega"] == [
        "domicilio",
        "recoger_en_tienda",
    ]
    r = await app_client.put(_url(agent_id), headers=h, json={"metodos_entrega": ["recoger_en_tienda"]})
    assert r.status_code == 200, r.text
    assert r.json()["metodos_entrega"] == ["recoger_en_tienda"]
    assert (await app_client.get(_url(agent_id), headers=h)).json()["metodos_entrega"] == ["recoger_en_tienda"]
    r = await app_client.put(_url(agent_id), headers=h, json={"metodos_entrega": []})
    assert r.status_code == 422


async def test_la_politica_de_un_agente_no_afecta_a_otro(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    agente_a, agente_b = await _agente(a), await _agente(b)
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    await app_client.put(
        _url(agente_a), headers=ha, json={"politica": {"ventas.pedido_retiro": {"id_document": "no_pedir"}}}
    )
    assert (
        _campo((await app_client.get(_url(agente_b), headers=hb)).json(), "ventas.pedido_retiro", "id_document")[
            "nivel"
        ]
        == "requerido"
    )
    assert (
        _campo((await app_client.get(_url(agente_a), headers=ha)).json(), "ventas.pedido_retiro", "id_document")[
            "nivel"
        ]
        == "no_pedir"
    )


async def test_dos_put_a_la_vez_de_acciones_distintas_no_se_pisan_a_medias(app_client) -> None:
    """El PUT reemplaza lo de las acciones enviadas: el último gana, pero el bloque nunca queda corrupto."""
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    agent_id = await _agente(tid)
    p1 = {"politica": {"ventas.pedido_domicilio": {"id_document": "no_pedir"}}}
    p2 = {"politica": {"ventas.pedido_retiro": {"id_document": "no_pedir"}}}
    r1, r2 = await asyncio.gather(
        app_client.put(_url(agent_id), headers=h, json=p1),
        app_client.put(_url(agent_id), headers=h, json=p2),
    )
    assert r1.status_code == 200 and r2.status_code == 200
    final = (await app_client.get(_url(agent_id), headers=h)).json()
    ajustados = [a["id"] for a in final["acciones"] for c in a["campos"] if c["ajustado"]]
    assert ajustados in (["ventas.pedido_domicilio"], ["ventas.pedido_retiro"])  # uno de los dos, entero


async def test_la_evaluacion_respeta_el_ajuste_del_agente_y_el_sobre_no_lleva_valores(app_client) -> None:
    """Sin ajuste pide el documento; con `no_pedir` ya no. El sobre lleva claves, nunca valores."""
    from core.datos_cliente.servicio import evaluar_accion
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    agent_id = await _agente(tid)
    datos = {
        "full_name": "Ana",
        "phone": "3001112233",
        "address": "Calle 1",
        "municipality": "Manizales",
        "city": "Manizales",
    }
    async with tenant_scope(tid):
        antes = await evaluar_accion("ventas.pedido_domicilio", datos, agent_id=agent_id)
    assert antes is not None and antes.faltan == ["id_document"]
    assert "Ana" not in str(antes.como_sobre())

    await app_client.put(
        _url(agent_id), headers=h, json={"politica": {"ventas.pedido_domicilio": {"id_document": "no_pedir"}}}
    )
    async with tenant_scope(tid):
        despues = await evaluar_accion("ventas.pedido_domicilio", datos, agent_id=agent_id)
    assert despues is not None and despues.faltan == [] and despues.puede_continuar


async def test_el_ajuste_cambia_la_lista_que_exige_ventas_y_la_tool_la_expone(app_client, monkeypatch) -> None:
    from pack_commerce.datos import A_LEGADO

    from core.datos_cliente import tools as tools_mod
    from core.datos_cliente.legado import requeridos_efectivos
    from core.datos_cliente.tools import DatosClienteSkill
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    agent_id = await _agente(tid)
    legado = ["full_name", "id_document", "address", "municipality", "city", "phone"]
    async with tenant_scope(tid):
        assert await requeridos_efectivos("ventas.pedido_domicilio", legado, A_LEGADO, agent_id=agent_id) == legado

    await app_client.put(
        _url(agent_id),
        headers=h,
        json={"politica": {"ventas.pedido_domicilio": {"id_document": "no_pedir", "email": "requerido"}}},
    )
    fake = SimpleNamespace(agent_id=agent_id, packs=frozenset({"pack_commerce"}), tools=frozenset({"crear_pedido"}))
    monkeypatch.setattr(tools_mod, "_agente", lambda: fake)
    ctx = SimpleNamespace(customer_id=uuid.uuid4(), agent_id=agent_id)
    async with tenant_scope(tid):
        efectivos = await requeridos_efectivos(
            "ventas.pedido_domicilio", legado, A_LEGADO, agent_id=agent_id
        )
        assert "id_document" not in efectivos and "email" in efectivos
        res = await DatosClienteSkill().consultar({"accion": "ventas.pedido_domicilio"}, ctx)
        todas = await DatosClienteSkill().consultar({}, ctx)
        desconocida = await DatosClienteSkill().consultar({"accion": "no.existe"}, ctx)
    [accion] = res["acciones"]
    assert "email" in accion["faltan"] and "id_document" not in accion["faltan"] + accion["ya_registrados"]
    assert {a["accion"] for a in todas["acciones"]} >= {"ventas.pedido_retiro"}
    assert "agenda.agendar_cita" not in {a["accion"] for a in todas["acciones"]}
    assert "error" in desconocida


async def test_las_herramientas_del_agente_nacen_apagadas_y_se_encienden_sin_tocar_los_ajustes(
    app_client, monkeypatch
) -> None:
    from core.datos_cliente import tools as tools_mod
    from core.datos_cliente.ajustes import herramientas_activas
    from core.datos_cliente.tools import _hay_acciones
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    agent_id = await _agente(tid)
    fake = SimpleNamespace(agent_id=agent_id, packs=frozenset({"pack_commerce"}), tools=frozenset({"crear_pedido"}))
    monkeypatch.setattr(tools_mod, "_agente", lambda: fake)
    assert (await app_client.get(_url(agent_id), headers=h)).json()["herramientas_del_agente"] is False
    async with tenant_scope(tid):
        assert await _hay_acciones(set()) is False  # apagado: el modelo no ve la tool

    await app_client.put(
        _url(agent_id),
        headers=h,
        json={"politica": {"ventas.pedido_retiro": {"id_document": "no_pedir"}}},
    )
    r = await app_client.put(
        _url(agent_id), headers=h, json={"herramientas_del_agente": True}
    )  # sin `politica`: no la toca
    assert r.json()["herramientas_del_agente"] is True
    assert _campo(r.json(), "ventas.pedido_retiro", "id_document")["nivel"] == "no_pedir"
    async with tenant_scope(tid):
        assert await _hay_acciones(set()) is True
        assert await herramientas_activas(agent_id) is True

    r = await app_client.put(_url(agent_id), headers=h, json={"herramientas_del_agente": False})
    assert r.json()["herramientas_del_agente"] is False
    async with tenant_scope(tid):
        assert await herramientas_activas(agent_id) is False
