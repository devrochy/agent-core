"""Roles de pack solo de packs activos en el negocio y quitar el perfil de gestor (agent-core#870).

Un pack «activo» en el negocio es el que está en su AgentSpec, no está apagado y tiene un especialista
publicado (la regla con la que el panel habilita el módulo). Cubre:
- `GET /pack-roles` no ofrece el gestor en un negocio con Cobranza en el spec pero sin agente.
- El alta y la invitación con `pack_cobranza:gestor` en ese negocio dan 400 y no crean el perfil.
- `POST /{id}/pack-roles/revoke` da de baja el perfil en el negocio activo y solo en él; la cuenta
  vuelve a la política de Colaborador y su equipo pendiente se aprueba en el próximo ingreso.
- Un dueño no quita perfiles de cuentas de otro negocio (misma respuesta que una inexistente).
Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import select
from test_access_dispositivos_core import PW, _evaluar, _h, _login

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/admin/collaborators"
GESTOR = "pack_cobranza:gestor"


async def _negocio(prefijo: str, *, con_agente_de_cobranza: bool) -> tuple[uuid.UUID, str]:
    """Negocio con `pack_cobranza` en el spec; con o sin especialista de Cobranza publicado."""
    from core.alta_negocio import onboard_tenant

    slug = uniq(prefijo)
    email = f"{slug}@test.local"
    tid, _ = await onboard_tenant(
        name=f"Negocio {slug}",
        slug=slug,
        business_description="Negocio de prueba de roles de pack.",
        persona={"tone": "cercano"},
        admin_email=email,
        admin_password=PW,
        packs=["pack_cobranza"],
        plan="premium",
    )
    if con_agente_de_cobranza:
        await _publicar_cobranza(tid)
    return tid, email


async def _publicar_cobranza(tid: uuid.UUID, *, habilitada: bool = True) -> None:
    """Especialista `cobranza` (plantilla del mismo nombre) con una instancia en una conexión."""
    from core.db.models import AgentInstanceRecord, AgentRecord, ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        agente = AgentRecord(tenant_id=tid, key="cobranza", kind="specialist", name="Cobranza", source_template="cobranza")
        conexion = ChannelConnection(
            tenant_id=tid, channel="instagram", provider="meta", external_id=f"ig-{uuid.uuid4().hex[:10]}",
            type_id="instagram_meta",
        )
        session.add_all([agente, conexion])
        await session.flush()
        session.add(
            AgentInstanceRecord(
                tenant_id=tid, connection_id=conexion.id, agent_id=agente.id, role="specialist", enabled=habilitada
            )
        )


async def _admin_id(email: str) -> uuid.UUID:
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(select(Admin.id).where(Admin.email == email))).scalar_one()


async def _gestores(tid: uuid.UUID, admin_id: uuid.UUID) -> list[tuple[str, bool]]:
    """(estatus, vivo) de los perfiles `Gestor` de la cuenta en el negocio."""
    from pack_cobranza.models import Gestor

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        filas = (
            await session.execute(select(Gestor).where(Gestor.tenant_id == tid, Gestor.admin_id == admin_id))
        ).scalars().all()
    return [(g.estatus, g.deleted_at is None) for g in filas]


async def _alta(app_client, token: str, tid: uuid.UUID, email: str, pack_roles: list[str]):
    return await app_client.post(
        BASE,
        json={"email": email, "password": PW, "role": "colaborador", "pack_roles": pack_roles},
        headers=_h(token, tid),
    )


async def test_sin_agente_de_cobranza_no_se_ofrece_ni_se_acepta_el_gestor(app_client) -> None:
    tid, owner = await _negocio("r870-sin", con_agente_de_cobranza=False)
    token = await _login(app_client, owner)

    r = await app_client.get(f"{BASE}/pack-roles", headers=_h(token, tid))
    assert r.status_code == 200, r.text
    assert r.json() == []

    nuevo = f"{uniq('r870-nuevo')}@test.local"
    r = await _alta(app_client, token, tid, nuevo, [GESTOR])
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "collaborator_error"
    # No se creó la cuenta ni el perfil: el rechazo va antes de escribir.
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        assert (await session.execute(select(Admin.id).where(Admin.email == nuevo))).scalar_one_or_none() is None

    # Una instancia apagada tampoco publica el módulo.
    await _publicar_cobranza(tid, habilitada=False)
    r = await app_client.get(f"{BASE}/pack-roles", headers=_h(token, tid))
    assert r.json() == []


async def test_la_invitacion_con_rol_de_pack_inactivo_se_rechaza(app_client) -> None:
    tid, owner = await _negocio("r870-inv", con_agente_de_cobranza=False)
    otro_tid, otro_owner = await _negocio("r870-inv-b", con_agente_de_cobranza=True)
    token = await _login(app_client, owner)
    # Una cuenta que ya existe (dueña de otro negocio) se invita, no se crea.
    r = await app_client.post(
        "/api/v1/admin/invitations",
        json={"email": otro_owner, "role": "colaborador", "pack_roles": [GESTOR]},
        headers=_h(token, tid),
    )
    assert r.status_code == 400, r.text
    from core.db.models import TenantInvitation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        assert (
            await session.execute(select(TenantInvitation.id).where(TenantInvitation.tenant_id == tid))
        ).first() is None
    assert await _gestores(tid, await _admin_id(otro_owner)) == []
    assert otro_tid != tid


async def test_con_agente_publicado_se_ofrece_y_un_rol_no_declarado_se_rechaza(app_client) -> None:
    tid, owner = await _negocio("r870-con", con_agente_de_cobranza=True)
    token = await _login(app_client, owner)

    r = await app_client.get(f"{BASE}/pack-roles", headers=_h(token, tid))
    assert r.json() == [{"value": GESTOR, "label": "Gestor de cobranza"}]

    r = await _alta(app_client, token, tid, f"{uniq('r870-x')}@test.local", ["pack_cobranza:inventado"])
    assert r.status_code == 400, r.text

    gestor = f"{uniq('r870-g')}@test.local"
    r = await _alta(app_client, token, tid, gestor, [GESTOR])
    assert r.status_code == 200, r.text
    assert await _gestores(tid, await _admin_id(gestor)) == [("activo", True)]


async def test_quitar_el_gestor_vuelve_a_colaborador_y_aprueba_su_equipo(app_client) -> None:
    from core.access import policy
    from core.access.roles import rol_de_acceso
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, owner = await _negocio("r870-quitar", con_agente_de_cobranza=True)
    token = await _login(app_client, owner)
    gestor = f"{uniq('r870-g')}@test.local"
    assert (await _alta(app_client, token, tid, gestor, [GESTOR])).status_code == 200
    aid = await _admin_id(gestor)
    # Como gestor (política restringida) su teléfono queda pendiente.
    async with tenant_scope(tid), get_session() as session:
        await policy.escribir(session, tid, {"gestor": "restringido", "colaborador": "abierto"})
    decision = await _evaluar(tid, aid, "gestor", "tel-870")
    assert not decision.permitido and decision.estado == "pendiente"

    r = await app_client.post(f"{BASE}/{aid}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token, tid))
    assert r.status_code == 204, r.text

    assert await _gestores(tid, aid) == [("baja", False)]
    async with tenant_scope(tid), get_session() as session:
        assert await rol_de_acceso(session, tid, aid) == "colaborador"
    decision = await _evaluar(tid, aid, "colaborador", "tel-870")
    assert decision.permitido and decision.estado == "activo"
    lista = (await app_client.get(BASE, headers=_h(token, tid))).json()
    assert next(i for i in lista if i["email"] == gestor)["pack_profiles"] == []

    # Volver a darle el rol revive el MISMO perfil (con su historial).
    from core.admin.collaborators_service import apply_pack_roles

    await apply_pack_roles([GESTOR], tid, aid)
    assert await _gestores(tid, aid) == [("activo", True)]


async def test_quitar_el_gestor_en_un_negocio_no_toca_el_de_otro(app_client) -> None:
    """Misma cuenta gestora en A y B: quitarle el perfil en A deja intacto el de B."""
    from core.admin.collaborators_service import apply_pack_roles, ensure_tenant_link

    tid_a, owner_a = await _negocio("r870-a", con_agente_de_cobranza=True)
    tid_b, _ = await _negocio("r870-b", con_agente_de_cobranza=True)
    token_a = await _login(app_client, owner_a)
    gestor = f"{uniq('r870-ab')}@test.local"
    assert (await _alta(app_client, token_a, tid_a, gestor, [GESTOR])).status_code == 200
    aid = await _admin_id(gestor)
    await ensure_tenant_link(aid, tid_b, "colaborador")
    await apply_pack_roles([GESTOR], tid_b, aid)

    r = await app_client.post(f"{BASE}/{aid}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token_a, tid_a))
    assert r.status_code == 204, r.text

    assert await _gestores(tid_a, aid) == [("baja", False)]
    assert await _gestores(tid_b, aid) == [("activo", True)]
    # Por la API: A ya no lo ve como gestor; B sí.
    lista_a = (await app_client.get(BASE, headers=_h(token_a, tid_a))).json()
    assert next(i for i in lista_a if i["email"] == gestor)["pack_profiles"] == []
    lista_b = (await app_client.get(BASE, headers=await api_headers(app_client, tid_b))).json()
    [perfil_b] = next(i for i in lista_b if i["email"] == gestor)["pack_profiles"]
    assert perfil_b["role_id"] == "gestor"


async def test_el_master_solo_quita_perfiles_en_el_negocio_activo(app_client) -> None:
    tid_a, _ = await _negocio("r870-m-a", con_agente_de_cobranza=True)
    tid_b, owner_b = await _negocio("r870-m-b", con_agente_de_cobranza=True)
    token_b = await _login(app_client, owner_b)
    gestor_b = f"{uniq('r870-mg')}@test.local"
    assert (await _alta(app_client, token_b, tid_b, gestor_b, [GESTOR])).status_code == 200
    aid = await _admin_id(gestor_b)
    master_a = await api_headers(app_client, tid_a)

    ajeno = await app_client.post(f"{BASE}/{aid}/pack-roles/revoke", json={"value": GESTOR}, headers=master_a)
    inexistente = await app_client.post(f"{BASE}/{uuid.uuid4()}/pack-roles/revoke", json={"value": GESTOR}, headers=master_a)

    assert ajeno.status_code == inexistente.status_code == 400
    assert ajeno.json()["error"]["message"] == inexistente.json()["error"]["message"]
    assert await _gestores(tid_b, aid) == [("activo", True)]


async def test_una_cuenta_eliminada_en_otro_negocio_es_indistinguible(app_client) -> None:
    tid_a, owner_a = await _negocio("r870-el-a", con_agente_de_cobranza=True)
    tid_b, owner_b = await _negocio("r870-el-b", con_agente_de_cobranza=True)
    token_b = await _login(app_client, owner_b)
    gestor_b = f"{uniq('r870-eg')}@test.local"
    assert (await _alta(app_client, token_b, tid_b, gestor_b, [GESTOR])).status_code == 200
    aid = await _admin_id(gestor_b)
    r = await app_client.post(f"{BASE}/{aid}/delete", json={}, headers=_h(token_b, tid_b))
    assert r.status_code == 204, r.text
    token_a = await _login(app_client, owner_a)

    ajeno = await app_client.post(f"{BASE}/{aid}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token_a, tid_a))
    inexistente = await app_client.post(
        f"{BASE}/{uuid.uuid4()}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token_a, tid_a)
    )
    propio = await app_client.post(f"{BASE}/{aid}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token_b, tid_b))

    assert ajeno.json()["error"]["message"] == inexistente.json()["error"]["message"] == "admin no encontrado"
    assert propio.status_code == 400 and "eliminada" in propio.json()["error"]["message"]


async def test_un_dueno_no_quita_el_gestor_de_una_cuenta_de_otro_negocio(app_client) -> None:
    tid_a, owner_a = await _negocio("r870-ajeno-a", con_agente_de_cobranza=True)
    tid_b, owner_b = await _negocio("r870-ajeno-b", con_agente_de_cobranza=True)
    token_b = await _login(app_client, owner_b)
    gestor_b = f"{uniq('r870-gb')}@test.local"
    assert (await _alta(app_client, token_b, tid_b, gestor_b, [GESTOR])).status_code == 200
    aid = await _admin_id(gestor_b)
    token_a = await _login(app_client, owner_a)

    ajeno = await app_client.post(f"{BASE}/{aid}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token_a, tid_a))
    inexistente = await app_client.post(
        f"{BASE}/{uuid.uuid4()}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token_a, tid_a)
    )

    assert ajeno.status_code == inexistente.status_code == 400
    assert ajeno.json()["error"]["message"] == inexistente.json()["error"]["message"]
    assert await _gestores(tid_b, aid) == [("activo", True)]
    # Tampoco puede quitarle nada al propietario de su propio negocio ni pasar un rol inventado.
    owner_a_id = await _admin_id(owner_a)
    r = await app_client.post(f"{BASE}/{owner_a_id}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token_a, tid_a))
    assert r.status_code == 400
    r = await app_client.post(f"{BASE}/{aid}/pack-roles/revoke", json={"value": "pack_x:gestor"}, headers=_h(token_b, tid_b))
    assert r.status_code == 400


async def test_quitar_el_gestor_funciona_aunque_cobranza_ya_no_este_activo(app_client) -> None:
    """El caso de #870: el perfil existe, pero el negocio no tiene agente de Cobranza publicado. Equipo lo
    muestra y se puede quitar."""
    from core.admin.collaborators_service import apply_pack_roles, ensure_tenant_link

    tid, owner = await _negocio("r870-caso", con_agente_de_cobranza=False)
    token = await _login(app_client, owner)
    colab = f"{uniq('r870-colab')}@test.local"
    assert (await _alta(app_client, token, tid, colab, [])).status_code == 200
    aid = await _admin_id(colab)
    await ensure_tenant_link(aid, tid, "colaborador")
    await apply_pack_roles([GESTOR], tid, aid)  # como quedó `colab-qa` antes del arreglo

    lista = (await app_client.get(BASE, headers=_h(token, tid))).json()
    [perfil] = next(i for i in lista if i["email"] == colab)["pack_profiles"]
    assert (perfil["pack"], perfil["role_id"]) == ("pack_cobranza", "gestor")

    r = await app_client.post(f"{BASE}/{aid}/pack-roles/revoke", json={"value": GESTOR}, headers=_h(token, tid))
    assert r.status_code == 204, r.text
    assert await _gestores(tid, aid) == [("baja", False)]
