"""HU-CRM-13 (ADR-0179): API de la política de datos del cliente y su efecto en Ventas."""

from __future__ import annotations

import asyncio
import os

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

URL = "/api/v1/admin/datos-cliente/politica"
PASSWORD = "Colab-Crm13-2026!"


async def _negocio(packs: list[str] | None = None):
    from core.alta_negocio import onboard_tenant

    slug = uniq("crm13")
    tid, _ = await onboard_tenant(
        name=f"N {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=packs or ["pack_commerce", "pack_calendar"],
        sembrar_ejemplos=False,
    )
    return tid


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


async def test_get_lista_las_acciones_de_los_modulos_habilitados_con_su_nivel(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    r = await app_client.get(URL, headers=h)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    ids = {a["id"] for a in cuerpo["acciones"]}
    assert {"ventas.pedido_domicilio", "ventas.pedido_retiro", "agenda.agendar_cita"} <= ids
    assert "suscripciones.inscribirse" not in ids  # el negocio no tiene ese módulo
    doc = _campo(cuerpo, "ventas.pedido_domicilio", "id_document")
    assert doc["nivel"] == "requerido" and doc["ajustado"] is False
    assert _campo(cuerpo, "agenda.agendar_cita", "email")["ajustable"] is False


async def test_put_guarda_solo_lo_que_difiere_audita_y_afecta_la_lectura(app_client) -> None:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    cuerpo = {"politica": {"ventas.pedido_domicilio": {"id_document": "no_pedir", "name": "requerido"}}}
    r = await app_client.put(URL, headers=h, json=cuerpo)
    assert r.status_code == 200, r.text
    salida = r.json()
    doc = _campo(salida, "ventas.pedido_domicilio", "id_document")
    assert doc["nivel"] == "no_pedir" and doc["ajustado"] is True and doc["por_defecto"] == "requerido"
    assert _campo(salida, "ventas.pedido_domicilio", "name")["ajustado"] is False  # igual al módulo: no se guarda

    async with tenant_scope(tid), get_session() as s:
        filas = list(
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == "settings.datos_cliente")
                )
            ).scalars()
        )
    assert len(filas) == 1
    assert filas[0].input["despues"] == {"ventas.pedido_domicilio": {"id_document": "no_pedir"}}

    # Volver a mandar lo mismo no deja otra fila de auditoría; vacío restablece.
    await app_client.put(URL, headers=h, json=cuerpo)
    r = await app_client.put(URL, headers=h, json={"politica": {}})
    assert _campo(r.json(), "ventas.pedido_domicilio", "id_document")["nivel"] == "requerido"


async def test_put_rechaza_acciones_datos_y_pisos_que_no_se_pueden_ajustar(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    for politica in (
        {"suscripciones.inscribirse": {"name": "no_pedir"}},  # módulo no habilitado
        {"ventas.pedido_domicilio": {"inventado": "no_pedir"}},
        {"agenda.agendar_cita": {"email": "no_pedir"}},  # piso técnico
        {"ventas.pedido_domicilio": {"name": "obligatorio"}},  # nivel inválido
    ):
        r = await app_client.put(URL, headers=h, json={"politica": politica})
        assert r.status_code == 422, (politica, r.text)


async def test_solo_quien_puede_escribir_en_configuracion_guarda(app_client) -> None:
    tid = await _negocio()
    lector = await _colaborador(tid, "read")
    h = await api_headers(app_client, tid, email=lector, password=PASSWORD)
    assert (await app_client.get(URL, headers=h)).status_code == 200
    r = await app_client.put(URL, headers=h, json={"politica": {}})
    assert r.status_code == 403


async def test_la_politica_de_un_negocio_no_afecta_a_otro(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    await app_client.put(URL, headers=ha, json={"politica": {"ventas.pedido_retiro": {"id_document": "no_pedir"}}})
    assert _campo((await app_client.get(URL, headers=hb)).json(), "ventas.pedido_retiro", "id_document")["nivel"] == "requerido"
    assert _campo((await app_client.get(URL, headers=ha)).json(), "ventas.pedido_retiro", "id_document")["nivel"] == "no_pedir"


async def test_dos_put_a_la_vez_de_acciones_distintas_no_se_pisan_a_medias(app_client) -> None:
    """El PUT reemplaza lo de las acciones enviadas: el último gana, pero el bloque nunca queda corrupto."""
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    p1 = {"politica": {"ventas.pedido_domicilio": {"id_document": "no_pedir"}}}
    p2 = {"politica": {"ventas.pedido_retiro": {"id_document": "no_pedir"}}}
    r1, r2 = await asyncio.gather(app_client.put(URL, headers=h, json=p1), app_client.put(URL, headers=h, json=p2))
    assert r1.status_code == 200 and r2.status_code == 200
    final = (await app_client.get(URL, headers=h)).json()
    ajustados = [
        a["id"] for a in final["acciones"] for c in a["campos"] if c["ajustado"]
    ]
    assert ajustados in (["ventas.pedido_domicilio"], ["ventas.pedido_retiro"])  # uno de los dos, entero


async def test_la_evaluacion_respeta_el_ajuste_del_dueno_y_el_sobre_no_lleva_valores(app_client) -> None:
    """Sin ajuste pide el documento; con `no_pedir` ya no. El sobre lleva claves, nunca valores."""
    from core.datos_cliente.servicio import evaluar_accion
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    datos = {"full_name": "Ana", "phone": "3001112233", "address": "Calle 1", "municipality": "Manizales", "city": "Manizales"}
    async with tenant_scope(tid):
        antes = await evaluar_accion("ventas.pedido_domicilio", datos)
    assert antes is not None and antes.faltan == ["id_document"]
    assert "Ana" not in str(antes.como_sobre())

    await app_client.put(URL, headers=h, json={"politica": {"ventas.pedido_domicilio": {"id_document": "no_pedir"}}})
    async with tenant_scope(tid):
        despues = await evaluar_accion("ventas.pedido_domicilio", datos)
    assert despues is not None and despues.faltan == [] and despues.puede_continuar


async def test_el_ajuste_cambia_la_lista_que_exige_ventas_y_la_tool_la_expone(app_client) -> None:
    import uuid
    from types import SimpleNamespace

    from pack_commerce.datos import A_LEGADO

    from core.datos_cliente.legado import requeridos_efectivos
    from core.datos_cliente.tools import DatosClienteSkill
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    legado = ["full_name", "id_document", "address", "municipality", "city", "phone"]
    async with tenant_scope(tid):
        assert await requeridos_efectivos("ventas.pedido_domicilio", legado, A_LEGADO) == legado

    await app_client.put(
        URL, headers=h, json={"politica": {"ventas.pedido_domicilio": {"id_document": "no_pedir", "email": "requerido"}}}
    )
    async with tenant_scope(tid):
        efectivos = await requeridos_efectivos("ventas.pedido_domicilio", legado, A_LEGADO)
        assert "id_document" not in efectivos and "email" in efectivos
        res = await DatosClienteSkill().consultar(
            {"accion": "ventas.pedido_domicilio"}, SimpleNamespace(customer_id=uuid.uuid4())
        )
        todas = await DatosClienteSkill().consultar({}, SimpleNamespace(customer_id=uuid.uuid4()))
        desconocida = await DatosClienteSkill().consultar({"accion": "no.existe"}, SimpleNamespace(customer_id=uuid.uuid4()))
    [accion] = res["acciones"]
    assert "email" in accion["faltan"] and "id_document" not in accion["faltan"] + accion["ya_registrados"]
    assert {a["accion"] for a in todas["acciones"]} >= {"ventas.pedido_retiro", "agenda.agendar_cita"}
    assert "error" in desconocida
