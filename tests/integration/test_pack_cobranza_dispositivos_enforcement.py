"""Vinculación de dispositivo del gestor (ADR-0068): enforcement, alta y administración.

Red de seguridad previa a mover «Dispositivos» al core. Cubre:

- el 403 por dispositivo no autorizado (lo prometía el docstring de
  `test_gestor_token_exchange.py` sin existir),
- el hueco por el que un dispositivo **bloqueado** abría el acceso,
- el 500 por IMEI repetido entre dos gestores del mismo negocio (ahora 409),
- `POST/GET /gestor/dispositivo`, `GET /devices`, `POST /devices/{id}/status` y `GET /alerts`.

Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"
EXCHANGE = "/api/v1/pack/cobranza/gestor-token"
CAMPO = "/api/pack/pack_cobranza/gestor/dispositivo"
ADMIN = "/api/v1/pack/cobranza"


async def _tenant(email: str, slug: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de dispositivos.",
        persona={"tone": "cercano"},
        admin_email=email,
        admin_password=PW,
        packs=["pack_cobranza"],
    )
    return tid


async def _politica_del_gestor(tenant_id: uuid.UUID, valor: str) -> None:
    from core.access import policy
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await policy.escribir(session, tenant_id, {"gestor": valor})


async def _sesion_sin_equipo(app_client, tenant_id: uuid.UUID, token: str) -> dict[str, str]:
    """Sesión de campo de un gestor que no identifica su equipo (token sin `did`). En política
    restringida ese login es 403 `dispositivo_requerido`, así que se abre con la política abierta y se
    vuelve a restringir: queda el token con el que una app sin id llama a las rutas de vinculación."""
    await _politica_del_gestor(tenant_id, "abierto")
    r = await app_client.post(EXCHANGE, json={}, headers=_h(token, tenant_id))
    await _politica_del_gestor(tenant_id, "restringido")
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}


async def _login_admin(app_client, identifier: str) -> str:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": identifier, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def _gestor(email: str, tenant_id: uuid.UUID, nombre: str = "Gestor Dev") -> uuid.UUID:
    """Admin colaborador + perfil `Gestor` activo (ADR-0066). Devuelve el id del `Gestor`."""
    from pack_cobranza.models import Gestor

    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        admin_id = admin.id
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role="colaborador"))
            gestor = Gestor(tenant_id=tenant_id, admin_id=admin_id, nombre=nombre)
            session.add(gestor)
            await session.flush()
            return gestor.id


async def _device(
    tenant_id: uuid.UUID, gestor_id: uuid.UUID, imei: str | None, estado: str
) -> uuid.UUID:
    from pack_cobranza.models import Device

    from core.admin.auth import hash_password
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            d = Device(
                tenant_id=tenant_id,
                codigo=str(uuid.uuid4()),
                api_key_hash=hash_password(uuid.uuid4().hex),
                gestor_id=gestor_id,
                imei=imei,
                whatsapp_number="3000000000",
                estado=estado,
            )
            session.add(d)
            await session.flush()
            return d.id


async def _eventos(tenant_id: uuid.UUID, gestor_id: uuid.UUID) -> list[str]:
    """Tipos de evento de acceso de la cuenta del gestor (core, ADR-0174)."""
    from pack_cobranza.models import Gestor
    from sqlalchemy import select

    from core.db.models import AccessEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            admin_id = (
                await session.execute(select(Gestor.admin_id).where(Gestor.id == gestor_id))
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(AccessEvent.tipo).where(
                            AccessEvent.tenant_id == tenant_id,
                            AccessEvent.admin_id == admin_id,
                            AccessEvent.tipo != "legado_pack",
                        )
                    )
                )
                .scalars()
                .all()
            )
    return list(rows)


async def _equipo_core(
    tenant_id: uuid.UUID, gestor_id: uuid.UUID, device_id: str | None, estado: str
) -> uuid.UUID:
    """Fila de `account_devices` de la cuenta del gestor (lo que ve el panel tras la migración)."""
    from pack_cobranza.models import Gestor
    from sqlalchemy import select

    from core.db.models import AccountDevice
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            admin_id = (
                await session.execute(select(Gestor.admin_id).where(Gestor.id == gestor_id))
            ).scalar_one()
            fila = AccountDevice(
                tenant_id=tenant_id,
                admin_id=admin_id,
                device_id=device_id,
                estado=estado,
                origen="alta_manual",
                legacy=device_id is None,
            )
            session.add(fila)
            await session.flush()
            return fila.id


def _h(token: str, tenant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}


async def _montar(app_client, prefijo: str):
    """Negocio + dueño + un gestor. Devuelve (tenant_id, gestor_id, token_gestor)."""
    slug = uniq(prefijo)
    tid = await _tenant(f"{slug}-owner@test.local", slug)
    gestor_email = f"{slug}-gestor@test.local"
    gid = await _gestor(gestor_email, tid)
    return tid, gid, await _login_admin(app_client, gestor_email)


async def test_dispositivo_no_autorizado_es_403_y_queda_registrado(app_client) -> None:
    tid, gid, token = await _montar(app_client, "dv-noaut")
    await _device(tid, gid, "IMEI-ACTIVO", "activo")

    r = await app_client.post(
        EXCHANGE, json={"imei": "IMEI-OTRO", "whatsapp": "3001112222"}, headers=_h(token, tid)
    )

    assert r.status_code == 403, r.text
    assert "dispositivo_pendiente" in await _eventos(tid, gid)  # queda registrado el equipo desconocido


async def test_dispositivo_activo_entra(app_client) -> None:
    tid, gid, token = await _montar(app_client, "dv-ok")
    await _device(tid, gid, "IMEI-ACTIVO", "activo")

    r = await app_client.post(
        EXCHANGE, json={"imei": "IMEI-ACTIVO", "whatsapp": "3001112222"}, headers=_h(token, tid)
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dispositivoVinculado"] is True
    assert body["estadoDispositivo"] == "activo"
    assert body["requiereVinculacion"] is False


async def test_dispositivo_bloqueado_unico_no_abre_el_acceso(app_client) -> None:
    """Regresión: con un único dispositivo `bloqueado`, `activos_imei` quedaba vacío y pasaba."""
    tid, gid, token = await _montar(app_client, "dv-bloq")
    await _device(tid, gid, "IMEI-BLOQ", "bloqueado")

    r = await app_client.post(
        EXCHANGE, json={"imei": "IMEI-BLOQ", "whatsapp": "3001112222"}, headers=_h(token, tid)
    )

    assert r.status_code == 403, r.text
    assert r.json()["code"] == "dispositivo_bloqueado"
    assert "dispositivo_bloqueado_usado" in await _eventos(tid, gid)


async def test_bloqueado_no_se_levanta_por_tener_otro_activo(app_client) -> None:
    tid, gid, token = await _montar(app_client, "dv-bloq2")
    await _device(tid, gid, "IMEI-BLOQ", "bloqueado")
    await _device(tid, gid, "IMEI-BUENO", "activo")

    bloqueado = await app_client.post(
        EXCHANGE, json={"imei": "IMEI-BLOQ", "whatsapp": "3001112222"}, headers=_h(token, tid)
    )
    bueno = await app_client.post(
        EXCHANGE, json={"imei": "IMEI-BUENO", "whatsapp": "3001112222"}, headers=_h(token, tid)
    )

    assert bloqueado.status_code == 403
    assert bueno.status_code == 200


async def test_vincular_crea_pendiente_y_es_idempotente(app_client) -> None:
    tid, _, token = await _montar(app_client, "dv-vinc")
    campo = await _sesion_sin_equipo(app_client, tid, token)
    cuerpo = {"imei": "IMEI-NUEVO", "whatsapp": "3001112222"}

    primera = await app_client.post(CAMPO, json=cuerpo, headers=campo)
    segunda = await app_client.post(CAMPO, json=cuerpo, headers=campo)
    estado = await app_client.get(CAMPO, headers=campo)

    assert primera.status_code == 201, primera.text
    assert primera.json()["estado"] == "pendiente"
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["estado"] == "pendiente"
    assert estado.json()["estado"] == "pendiente"
    assert estado.json()["vinculado"] is False


async def test_vincular_exige_imei_y_whatsapp(app_client) -> None:
    tid, _, token = await _montar(app_client, "dv-vacio")
    campo = await _sesion_sin_equipo(app_client, tid, token)

    r = await app_client.post(CAMPO, json={"imei": "", "whatsapp": ""}, headers=campo)

    assert r.status_code == 400


async def test_mismo_equipo_en_dos_cuentas_se_registra_sin_500(app_client) -> None:
    """Regresión del 500: el vínculo ahora es por cuenta, así que un equipo compartido entre dos gestores
    del negocio se permite (ADR-0174): la segunda cuenta queda pendiente y queda el aviso de compartido."""
    tid, gid, _ = await _montar(app_client, "dv-dup")
    await _equipo_core(tid, gid, "IMEI-COMPARTIDO", "activo")  # ya migrado del pack al core
    otro_email = f"{uniq('dv-dup2')}@test.local"
    otro_gid = await _gestor(otro_email, tid, nombre="Segundo")
    otro_token = await _login_admin(app_client, otro_email)
    campo = await _sesion_sin_equipo(app_client, tid, otro_token)

    r = await app_client.post(
        CAMPO, json={"imei": "IMEI-COMPARTIDO", "whatsapp": "3001112222"}, headers=campo
    )

    assert r.status_code == 201, r.text
    assert r.json()["estado"] == "pendiente"
    assert "dispositivo_compartido" in await _eventos(tid, otro_gid)


async def test_estado_sin_token_de_gestor_es_403(app_client) -> None:
    """El propietario no tiene `gestor_id`: no puede vincular equipo (límite actual)."""
    slug = uniq("dv-prop")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug)
    token = await _login_admin(app_client, email)
    sesion = await app_client.post(EXCHANGE, json={}, headers=_h(token, tid))
    assert sesion.status_code == 200, sesion.text
    campo = {"Authorization": f"Bearer {sesion.json()['accessToken']}"}

    r = await app_client.get(CAMPO, headers=campo)

    assert r.status_code == 403


async def test_admin_lista_aprueba_y_bloquea(app_client) -> None:
    slug = uniq("dv-adm")
    owner_email = f"{slug}-owner@test.local"
    tid = await _tenant(owner_email, slug)
    gid = await _gestor(f"{slug}-gestor@test.local", tid)
    pendiente = await _equipo_core(tid, gid, "IMEI-PEND", "pendiente")
    owner = await _login_admin(app_client, owner_email)

    lista = await app_client.get(f"{ADMIN}/devices", headers=_h(owner, tid))
    assert lista.status_code == 200, lista.text
    assert any(d["id"] == str(pendiente) for d in lista.json()["dispositivos"])
    assert lista.headers["Sunset"] and lista.headers["Deprecation"] == "true"

    ok = await app_client.post(
        f"{ADMIN}/devices/{pendiente}/status", json={"estado": "activo"}, headers=_h(owner, tid)
    )
    assert ok.status_code == 204, ok.text
    bloqueo = await app_client.post(
        f"{ADMIN}/devices/{pendiente}/status", json={"estado": "bloqueado"}, headers=_h(owner, tid)
    )
    assert bloqueo.status_code == 204, bloqueo.text
    invalido = await app_client.post(
        f"{ADMIN}/devices/{pendiente}/status", json={"estado": "inventado"}, headers=_h(owner, tid)
    )
    assert invalido.status_code == 400
    assert invalido.json()["error"]["code"] == "invalid_status"


async def test_alertas_muestra_el_intento_denegado(app_client) -> None:
    slug = uniq("dv-alr")
    owner_email = f"{slug}-owner@test.local"
    tid = await _tenant(owner_email, slug)
    gestor_email = f"{slug}-gestor@test.local"
    gid = await _gestor(gestor_email, tid)
    await _device(tid, gid, "IMEI-ACTIVO", "activo")
    gestor_token = await _login_admin(app_client, gestor_email)
    denegado = await app_client.post(
        EXCHANGE,
        json={"imei": "IMEI-AJENO", "whatsapp": "3001112222"},
        headers=_h(gestor_token, tid),
    )
    assert denegado.status_code == 403
    owner = await _login_admin(app_client, owner_email)

    r = await app_client.get(f"{ADMIN}/alerts", headers=_h(owner, tid))

    assert r.status_code == 200, r.text
    assert any(a["critico"] for a in r.json()["items"])


async def test_dispositivos_de_otro_negocio_no_se_ven(app_client) -> None:
    """Aislamiento entre negocios: la lista de un tenant no incluye dispositivos del otro."""
    slug_a, slug_b = uniq("dv-iso-a"), uniq("dv-iso-b")
    owner_a = f"{slug_a}@test.local"
    tid_a = await _tenant(owner_a, slug_a)
    tid_b = await _tenant(f"{slug_b}@test.local", slug_b)
    gid_b = await _gestor(f"{slug_b}-g@test.local", tid_b)
    ajeno = await _equipo_core(tid_b, gid_b, "IMEI-DE-B", "activo")
    token_a = await _login_admin(app_client, owner_a)

    r = await app_client.get(f"{ADMIN}/devices", headers=_h(token_a, tid_a))

    assert r.status_code == 200, r.text
    assert all(d["id"] != str(ajeno) for d in r.json()["dispositivos"])
    cambio = await app_client.post(
        f"{ADMIN}/devices/{ajeno}/status", json={"estado": "bloqueado"}, headers=_h(token_a, tid_a)
    )
    assert cambio.status_code == 404
