"""Login de campo del gestor sobre los dispositivos de la cuenta del core (HU-ACC-02, ADR-0174).

Cubre el contrato nuevo (header `X-Device-Id` → 403 con `code`), la convivencia con la app vieja (solo
`imei` en el cuerpo), la política por negocio, la adopción de lo que la migración no copió, la fila
heredada sin equipo y los wrappers del panel (`Sunset`, permiso viejo o nuevo).

Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select
from test_pack_cobranza_dispositivos_enforcement import (
    ADMIN,
    CAMPO,
    EXCHANGE,
    PW,
    _device,
    _eventos,
    _gestor,
    _h,
    _login_admin,
    _montar,
    _tenant,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _con_equipo(token: str, tid: uuid.UUID, equipo: str | None) -> dict[str, str]:
    h = _h(token, tid)
    if equipo:
        h["X-Device-Id"] = equipo
    return h


async def _filas(tenant_id: uuid.UUID, gestor_id: uuid.UUID) -> list:
    from pack_cobranza.models import Gestor

    from core.db.models import AccountDevice
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            admin_id = (
                await session.execute(select(Gestor.admin_id).where(Gestor.id == gestor_id))
            ).scalar_one()
            return list(
                (
                    await session.execute(
                        select(AccountDevice)
                        .where(
                            AccountDevice.tenant_id == tenant_id,
                            AccountDevice.admin_id == admin_id,
                            AccountDevice.deleted_at.is_(None),
                        )
                        .order_by(AccountDevice.created_at)
                    )
                )
                .scalars()
                .all()
            )


async def _politica_del_gestor(tenant_id: uuid.UUID, valor: str) -> None:
    from core.access import policy
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await policy.escribir(session, tenant_id, {"gestor": valor})


# ------------------------------------------------------------------ contrato nuevo (header)
async def test_restringido_pide_vinculacion_y_el_dueno_la_aprueba(app_client) -> None:
    slug = uniq("ac2-flujo")
    owner_email = f"{slug}-owner@test.local"
    tid = await _tenant(owner_email, slug)
    gestor_email = f"{slug}-gestor@test.local"
    gid = await _gestor(gestor_email, tid)
    token = await _login_admin(app_client, gestor_email)

    primero = await app_client.post(
        EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-NUEVO-1")
    )

    assert primero.status_code == 403, primero.text
    assert primero.json()["code"] == "dispositivo_pendiente"
    assert primero.json()["estado"] == "pendiente"
    assert "dispositivo_pendiente" in await _eventos(tid, gid)
    (fila,) = await _filas(tid, gid)
    assert (fila.estado, fila.device_id, fila.origen) == (
        "pendiente",
        "EQUIPO-NUEVO-1",
        "autoregistro",
    )

    owner = await _login_admin(app_client, owner_email)
    ok = await app_client.post(
        f"{ADMIN}/devices/{fila.id}/status", json={"estado": "activo"}, headers=_h(owner, tid)
    )
    assert ok.status_code == 204, ok.text
    assert ok.headers["Sunset"]

    segundo = await app_client.post(
        EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-NUEVO-1")
    )
    assert segundo.status_code == 200, segundo.text
    assert segundo.json()["dispositivoVinculado"] is True
    assert segundo.json()["requiereVinculacion"] is False


async def test_abierto_registra_y_permite(app_client) -> None:
    tid, gid, token = await _montar(app_client, "ac2-abierto")
    await _politica_del_gestor(tid, "abierto")

    r = await app_client.post(EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-ABIERTO"))

    assert r.status_code == 200, r.text
    (fila,) = await _filas(tid, gid)
    assert (fila.estado, fila.origen) == ("activo", "politica_abierta")
    assert "dispositivo_nuevo" in await _eventos(tid, gid)


@pytest.mark.parametrize("politica", ["restringido", "abierto"])
async def test_bloqueado_deniega_con_code_en_ambas_politicas(app_client, politica: str) -> None:
    tid, gid, token = await _montar(app_client, f"ac2-bloq-{politica}")
    await _politica_del_gestor(tid, politica)
    await _device(tid, gid, "EQUIPO-BLOQ", "bloqueado")

    r = await app_client.post(EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-BLOQ"))

    assert r.status_code == 403, r.text
    assert r.json()["code"] == "dispositivo_bloqueado"


async def test_con_header_el_equipo_no_vinculado_recibe_403_aunque_tenga_otro_activo(
    app_client,
) -> None:
    tid, gid, token = await _montar(app_client, "ac2-otro")
    await _device(tid, gid, "EQUIPO-VIEJO", "activo")

    r = await app_client.post(EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-OTRO"))

    assert r.status_code == 403
    assert r.json()["code"] == "dispositivo_pendiente"


# ------------------------------------------------------------------ app vieja (solo imei)
async def test_app_vieja_con_equipo_por_vincular_recibe_tokens_y_requiere_vinculacion(
    app_client,
) -> None:
    tid, gid, token = await _montar(app_client, "ac2-vieja")

    r = await app_client.post(
        EXCHANGE, json={"imei": "IMEI-VIEJA", "whatsapp": "3001112222"}, headers=_h(token, tid)
    )

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["accessToken"] and cuerpo["requiereVinculacion"] is True
    assert (cuerpo["dispositivoVinculado"], cuerpo["estadoDispositivo"]) == (False, "pendiente")
    (fila,) = await _filas(tid, gid)
    assert (fila.device_id, fila.estado) == ("IMEI-VIEJA", "pendiente")


async def test_app_vieja_sin_equipo_ni_imei_conserva_el_contrato(app_client) -> None:
    tid, _, token = await _montar(app_client, "ac2-vieja-sin")

    r = await app_client.post(EXCHANGE, json={}, headers=_h(token, tid))

    assert r.status_code == 200, r.text
    assert (r.json()["dispositivoVinculado"], r.json()["estadoDispositivo"]) == (False, "none")


# ------------------------------------------------------------------ lo que viene del pack
async def test_adopta_el_dispositivo_del_pack_que_la_migracion_no_copio(app_client) -> None:
    tid, gid, token = await _montar(app_client, "ac2-adopta")
    pack_id = await _device(tid, gid, "IMEI-DEL-PACK", "activo")

    primera = await app_client.post(
        EXCHANGE, json={}, headers=_con_equipo(token, tid, "IMEI-DEL-PACK")
    )
    segunda = await app_client.post(
        EXCHANGE, json={}, headers=_con_equipo(token, tid, "IMEI-DEL-PACK")
    )

    assert primera.status_code == 200 and segunda.status_code == 200
    (fila,) = await _filas(tid, gid)  # una sola fila: la segunda vez no re-adopta
    assert (fila.legacy_pack_device_id, fila.origen, fila.estado) == (
        pack_id,
        "migrado_pack",
        "activo",
    )


async def test_fila_heredada_sin_equipo_se_completa_con_el_primer_login(app_client) -> None:
    tid, gid, token = await _montar(app_client, "ac2-legacy")
    await _device(tid, gid, None, "activo")  # vínculo manual sin imei (ADR-0068)

    primero = await app_client.post(
        EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-REAL")
    )
    otro = await app_client.post(EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-AJENO"))

    assert primero.status_code == 200, primero.text
    assert otro.status_code == 403  # el equipo ya quedó atado; uno distinto pide vinculación
    filas = {f.device_id: f for f in await _filas(tid, gid)}
    assert filas["EQUIPO-REAL"].estado == "activo" and filas["EQUIPO-REAL"].legacy is False
    assert filas["EQUIPO-AJENO"].estado == "pendiente"


# ------------------------------------------------------------------ POST/GET /gestor/dispositivo
async def test_vincular_concurrente_no_da_500_y_deja_una_fila(app_client) -> None:
    tid, gid, token = await _montar(app_client, "ac2-conc")
    sesion = await app_client.post(EXCHANGE, json={}, headers=_h(token, tid))
    campo = {
        "Authorization": f"Bearer {sesion.json()['accessToken']}",
        "X-Device-Id": "EQUIPO-CONC",
    }

    respuestas = await asyncio.gather(
        *(app_client.post(CAMPO, json={}, headers=campo) for _ in range(4))
    )

    assert all(r.status_code in (200, 201) for r in respuestas), [r.text for r in respuestas]
    assert [f.device_id for f in await _filas(tid, gid)] == ["EQUIPO-CONC"]


async def test_estado_con_header_es_el_de_ese_equipo(app_client) -> None:
    tid, gid, token = await _montar(app_client, "ac2-estado")
    await _device(tid, gid, "EQUIPO-A", "activo")
    sesion = await app_client.post(EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-A"))
    assert sesion.status_code == 200, sesion.text
    campo = {"Authorization": f"Bearer {sesion.json()['accessToken']}"}

    de_a = await app_client.get(CAMPO, headers={**campo, "X-Device-Id": "EQUIPO-A"})
    de_b = await app_client.get(CAMPO, headers={**campo, "X-Device-Id": "EQUIPO-B"})

    assert de_a.json()["vinculado"] is True and de_a.json()["imei"] == "EQUIPO-A"
    assert de_b.json()["vinculado"] is False and de_b.json()["estado"] == "none"


# ------------------------------------------------------------------ wrappers del panel
async def test_wrappers_aceptan_el_permiso_viejo_o_el_nuevo(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    slug = uniq("ac2-perm")
    tid = await _tenant(f"{slug}-owner@test.local", slug)

    async def colaborador(sufijo: str, permisos: dict[str, str]) -> str:
        email = f"{slug}-{sufijo}@test.local"
        async with get_session() as session:
            admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
            session.add(admin)
            await session.flush()
            session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
            for seccion, acceso in permisos.items():
                session.add(
                    AdminPermission(
                        admin_id=admin.id, section=seccion, access=acceso, tenant_id=tid
                    )
                )
        return await _login_admin(app_client, email)

    viejo = await colaborador(
        "viejo", {"pack_cobranza:dispositivos": "read", "pack_cobranza:alertas": "read"}
    )
    nuevo = await colaborador("nuevo", {"devices": "read", "access_alerts": "read"})

    for token in (viejo, nuevo):
        assert (await app_client.get(f"{ADMIN}/devices", headers=_h(token, tid))).status_code == 200
        alertas = await app_client.get(f"{ADMIN}/alerts", headers=_h(token, tid))
        assert alertas.status_code == 200 and alertas.headers["Sunset"]
    # Solo lectura no cambia estados (ni con el permiso nuevo ni con el viejo).
    fila = uuid.uuid4()
    for token in (viejo, nuevo):
        r = await app_client.post(
            f"{ADMIN}/devices/{fila}/status", json={"estado": "activo"}, headers=_h(token, tid)
        )
        assert r.status_code == 403


async def test_el_listado_adopta_lo_que_la_migracion_no_copio(app_client) -> None:
    slug = uniq("ac2-lista")
    owner_email = f"{slug}-owner@test.local"
    tid = await _tenant(owner_email, slug)
    gid = await _gestor(f"{slug}-g@test.local", tid)
    await _device(tid, gid, "IMEI-SIN-COPIAR", "pendiente")
    owner = await _login_admin(app_client, owner_email)

    r = await app_client.get(f"{ADMIN}/devices", headers=_h(owner, tid))

    assert r.status_code == 200, r.text
    (d,) = [
        d for d in r.json()["dispositivos"] if d["imei"] == "COPIAR"
    ]  # solo el sufijo, como en el core
    assert (
        d["estado"] == "pendiente" and d["whatsapp"] == "3000000000" and d["gestor"] == "Gestor Dev"
    )
    assert r.json()["pendientes"] == 1


async def test_el_bloqueo_desde_el_wrapper_corta_el_login(app_client) -> None:
    slug = uniq("ac2-corta")
    owner_email = f"{slug}-owner@test.local"
    tid = await _tenant(owner_email, slug)
    gestor_email = f"{slug}-g@test.local"
    gid = await _gestor(gestor_email, tid)
    await _device(tid, gid, "EQUIPO-OK", "activo")
    token = await _login_admin(app_client, gestor_email)
    entra = await app_client.post(EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-OK"))
    assert entra.status_code == 200, entra.text
    (fila,) = await _filas(tid, gid)
    owner = await _login_admin(app_client, owner_email)

    bloqueo = await app_client.post(
        f"{ADMIN}/devices/{fila.id}/status", json={"estado": "bloqueado"}, headers=_h(owner, tid)
    )
    despues = await app_client.post(EXCHANGE, json={}, headers=_con_equipo(token, tid, "EQUIPO-OK"))

    assert bloqueo.status_code == 204, bloqueo.text
    assert despues.status_code == 403 and despues.json()["code"] == "dispositivo_bloqueado"


async def test_app_vieja_sin_imei_no_entra_si_la_cuenta_ya_tiene_un_equipo_activo(
    app_client,
) -> None:
    """Quien tiene usuario y clave de un gestor ya vinculado no entra omitiendo el equipo."""
    tid, gid, token = await _montar(app_client, "ac2-omite")
    await _device(tid, gid, "EQUIPO-DEL-GESTOR", "activo")

    r = await app_client.post(EXCHANGE, json={}, headers=_h(token, tid))

    assert r.status_code == 403, r.text
    assert "accessToken" not in r.json()


async def test_un_none_explicito_en_la_seccion_nueva_no_se_salta_con_la_vieja(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    slug = uniq("ac2-none")
    tid = await _tenant(f"{slug}-owner@test.local", slug)
    email = f"{slug}-c@test.local"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        for seccion in ("devices", "access_alerts"):
            session.add(
                AdminPermission(admin_id=admin.id, section=seccion, access="none", tenant_id=tid)
            )
    token = await _login_admin(app_client, email)

    assert (await app_client.get(f"{ADMIN}/devices", headers=_h(token, tid))).status_code == 403
    assert (await app_client.get(f"{ADMIN}/alerts", headers=_h(token, tid))).status_code == 403


async def test_alertas_y_estado_solo_alcanzan_cuentas_de_gestor(app_client) -> None:
    from core.access.events import registrar_evento
    from core.db.models import AccountDevice
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("ac2-solo")
    owner_email = f"{slug}-owner@test.local"
    tid = await _tenant(owner_email, slug)
    from core.db.models import TenantUser

    async with get_session() as session:
        owner_id = (
            await session.execute(
                select(TenantUser.admin_id).where(
                    TenantUser.tenant_id == tid, TenantUser.role == "owner"
                )
            )
        ).scalar_one()
    async with tenant_scope(tid):
        async with get_session() as session:
            registrar_evento(
                session, tenant_id=tid, tipo="acceso_master", admin_id=owner_id, rol="owner"
            )
            fila = AccountDevice(
                tenant_id=tid,
                admin_id=owner_id,
                device_id="EQUIPO-DEL-DUENO",
                estado="activo",
                origen="alta_manual",
            )
            session.add(fila)
            await session.flush()
            fila_id = fila.id
    owner = await _login_admin(app_client, owner_email)

    alertas = await app_client.get(f"{ADMIN}/alerts", headers=_h(owner, tid))
    cambio = await app_client.post(
        f"{ADMIN}/devices/{fila_id}/status", json={"estado": "bloqueado"}, headers=_h(owner, tid)
    )

    assert (
        alertas.status_code == 200 and alertas.json()["total"] == 0
    )  # el evento del dueño no sale
    assert (
        cambio.status_code == 404
    )  # la ruta vieja no toca equipos de cuentas que no son de gestor
