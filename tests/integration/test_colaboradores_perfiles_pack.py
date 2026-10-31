"""Perfiles de pack en Equipo (ADR-0176, HU-ACC-07): hook `collaborator_profiles` y `pack_profiles`.

Cubre que el gestor aparece con su estatus, carteras y conteo de permisos; que la lista llama al hook una
vez por pack con todos los ids (sin una consulta por colaborador); que un pack roto o lento no tumba el
listado; el recorte por permiso; y el aislamiento entre negocios. Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select
from test_access_dispositivos_core import _h, _login
from test_pack_cobranza_dispositivos_enforcement import _gestor
from test_pack_cobranza_dispositivos_enforcement import _tenant as _tenant_pack

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

LISTA = "/api/v1/admin/collaborators"


async def _negocio(prefijo: str) -> tuple[uuid.UUID, str, str]:
    slug = uniq(prefijo)
    email = f"{slug}@test.local"
    return await _tenant_pack(email, slug), email, slug


async def _gestor_con_datos(tid: uuid.UUID, correo: str, *, permisos: list[str], carteras: list[str]) -> uuid.UUID:
    from pack_cobranza.models import Cartera, GestorPermiso

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    gestor_id = await _gestor(correo, tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            for permiso in permisos:
                session.add(GestorPermiso(tenant_id=tid, gestor_id=gestor_id, permiso=permiso, habilitado=True))
            for nombre in carteras:
                session.add(Cartera(tenant_id=tid, gestor_id=gestor_id, nombre=nombre))
    return gestor_id


def _perfil(items: list[dict], correo: str) -> list[dict]:
    return next(i for i in items if i["email"] == correo)["pack_profiles"]


async def test_el_gestor_aparece_con_estatus_carteras_y_permisos(app_client) -> None:
    tid, owner_email, slug = await _negocio("pp-ver")
    gestor_email = f"{slug}-g@test.local"
    gid = await _gestor_con_datos(
        tid, gestor_email, permisos=["registrar_pago", "ver_cartera"], carteras=["Ruta B", "Ruta A"]
    )
    token = await _login(app_client, owner_email)

    r = await app_client.get(LISTA, headers=_h(token, tid))

    assert r.status_code == 200, r.text
    [perfil] = _perfil(r.json(), gestor_email)
    assert perfil["pack"] == "pack_cobranza" and perfil["role_id"] == "gestor" and perfil["etiqueta"] == "Gestor"
    assert perfil["ref_id"] == str(gid)
    resumen = perfil["resumen"]
    assert resumen["estatus"] == "activo"
    assert [c["nombre"] for c in resumen["carteras"]] == ["Ruta A", "Ruta B"]
    assert resumen["permisos_activos"] == 2 and resumen["permisos_total"] == 14
    assert perfil["edit"] == {"kind": "cobranza_gestor_permisos", "ref_id": str(gid)}
    assert _perfil(r.json(), owner_email) == []  # quien no es gestor no muestra nada


async def test_una_llamada_al_hook_por_pack_con_todos_los_ids(app_client, monkeypatch) -> None:
    from pack_cobranza.module import CobranzaModule

    tid, owner_email, slug = await _negocio("pp-lote")
    for i in range(3):
        await _gestor_con_datos(tid, f"{slug}-g{i}@test.local", permisos=[], carteras=[])
    token = await _login(app_client, owner_email)
    llamadas: list[list[uuid.UUID]] = []
    original = CobranzaModule.collaborator_profiles

    async def espiar(self, tenant_id, admin_ids):
        llamadas.append(list(admin_ids))
        return await original(self, tenant_id, admin_ids)

    monkeypatch.setattr(CobranzaModule, "collaborator_profiles", espiar)

    r = await app_client.get(LISTA, headers=_h(token, tid))

    assert r.status_code == 200
    assert len(llamadas) == 1 and len(llamadas[0]) == len(r.json()) == 4  # dueño + 3 gestores


async def test_un_pack_que_falla_o_se_pasa_de_tiempo_deja_el_listado_intacto(app_client, monkeypatch) -> None:
    import asyncio

    from pack_cobranza.module import CobranzaModule

    from core.admin import collaborators_service

    tid, owner_email, slug = await _negocio("pp-roto")
    gestor_email = f"{slug}-g@test.local"
    await _gestor_con_datos(tid, gestor_email, permisos=[], carteras=[])
    token = await _login(app_client, owner_email)

    async def roto(self, tenant_id, admin_ids):
        raise RuntimeError("boom")

    monkeypatch.setattr(CobranzaModule, "collaborator_profiles", roto)
    r = await app_client.get(LISTA, headers=_h(token, tid))
    assert r.status_code == 200 and len(r.json()) == 2
    assert _perfil(r.json(), gestor_email) == []

    async def lento(self, tenant_id, admin_ids):
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(CobranzaModule, "collaborator_profiles", lento)
    monkeypatch.setattr(collaborators_service, "PERFILES_TIMEOUT_S", 0.05)
    r = await app_client.get(LISTA, headers=_h(token, tid))
    assert r.status_code == 200 and len(r.json()) == 2
    assert _perfil(r.json(), gestor_email) == []


async def test_sin_acceso_al_pack_se_ve_que_es_gestor_pero_no_el_detalle(app_client) -> None:
    """`collaborators` es una sección solo del dueño, así que el recorte es una defensa del servicio: se prueba
    ahí, con quien pregunta sin acceso a ninguna sección del pack."""
    from core.admin.collaborators_service import perfiles_de_pack
    from core.db.models import Admin
    from core.db.session import get_session

    tid, _, slug = await _negocio("pp-recorte")
    gestor_email = f"{slug}-g@test.local"
    await _gestor_con_datos(tid, gestor_email, permisos=["ver_cartera"], carteras=["Ruta A"])
    async with get_session() as session:
        admin_id = (await session.execute(select(Admin.id).where(Admin.email == gestor_email))).scalar_one()

    sin = (await perfiles_de_pack(tid, [admin_id], ver_detalle=lambda _p: False))[admin_id][0]
    con = (await perfiles_de_pack(tid, [admin_id], ver_detalle=lambda p: p == "pack_cobranza"))[admin_id][0]

    assert sin.etiqueta == "Gestor" and sin.role_id == "gestor" and sin.pack == "pack_cobranza"
    assert sin.resumen is None and sin.edit is None
    assert con.resumen is not None and con.edit is not None


async def test_cada_negocio_solo_ve_a_sus_gestores(app_client) -> None:
    tid_a, owner_a, slug_a = await _negocio("pp-aisla-a")
    tid_b, owner_b, slug_b = await _negocio("pp-aisla-b")
    gestor_a, gestor_b = f"{slug_a}-g@test.local", f"{slug_b}-g@test.local"
    await _gestor_con_datos(tid_a, gestor_a, permisos=["ver_cartera"], carteras=["De A"])
    await _gestor_con_datos(tid_b, gestor_b, permisos=[], carteras=["De B"])

    ra = await app_client.get(LISTA, headers=_h(await _login(app_client, owner_a), tid_a))
    rb = await app_client.get(LISTA, headers=_h(await _login(app_client, owner_b), tid_b))

    assert {i["email"] for i in ra.json()} == {owner_a, gestor_a}
    assert [c["nombre"] for c in _perfil(ra.json(), gestor_a)[0]["resumen"]["carteras"]] == ["De A"]
    assert [c["nombre"] for c in _perfil(rb.json(), gestor_b)[0]["resumen"]["carteras"]] == ["De B"]


async def test_el_hook_no_devuelve_cuentas_ajenas_a_la_peticion(app_client) -> None:
    """El core descarta perfiles de cuentas que no pidió, aunque un pack los devuelva."""
    from core.admin.collaborators_service import perfiles_de_pack

    tid, _, slug = await _negocio("pp-ajeno")
    await _gestor_con_datos(tid, f"{slug}-g@test.local", permisos=[], carteras=[])
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        ids = list(
            (await session.execute(select(Admin.id).where(Admin.email == f"{slug}-g@test.local"))).scalars()
        )

    todos = await perfiles_de_pack(tid, ids, ver_detalle=lambda _p: True)
    ninguno = await perfiles_de_pack(tid, [uuid.uuid4()], ver_detalle=lambda _p: True)

    assert set(todos) == set(ids) and ninguno == {}
