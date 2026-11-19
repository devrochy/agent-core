"""Dispositivos de la cuenta y política en el core (ADR-0174, HU-ACC-01).

Cubre el servicio `core.access.devices` (regla de decisión, alta idempotente, concurrencia), la RLS de
`account_devices`/`access_events` y las rutas `/api/v1/access/*` (listas, transiciones con estado esperado,
política, permisos de sección y aislamiento entre negocios). Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select, text, update

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"
ACCESS = "/api/v1/access"


# ------------------------------------------------------------------ montaje
async def _tenant(email: str, slug: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de dispositivos.",
        persona={"tone": "cercano"},
        admin_email=email,
        admin_password=PW,
        packs=[],
    )
    return tid


async def _montar(prefijo: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Negocio con su dueño. Devuelve (tenant_id, owner_admin_id, owner_email)."""
    from core.db.models import TenantUser
    from core.db.session import get_session

    slug = uniq(prefijo)
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug)
    async with get_session() as session:
        owner_id = (
            await session.execute(
                select(TenantUser.admin_id).where(
                    TenantUser.tenant_id == tid, TenantUser.role == "owner"
                )
            )
        ).scalar_one()
    return tid, owner_id, email


async def _colaborador(
    tid: uuid.UUID, slug: str, permisos: dict[str, str] | None = None
) -> tuple[uuid.UUID, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{slug}-colab@test.local"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        for seccion, acceso in (permisos or {}).items():
            session.add(
                AdminPermission(admin_id=admin.id, section=seccion, access=acceso, tenant_id=tid)
            )
        return admin.id, email


async def _login(app_client, identifier: str) -> str:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": identifier, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(token: str, tid: uuid.UUID, device: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tid)}
    if device:
        h["X-Device-Id"] = device
    return h


async def _evaluar(tid: uuid.UUID, admin_id: uuid.UUID, rol: str, device_id: str | None, **kw):
    from core.access import devices
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await devices.evaluar(
                session, tenant_id=tid, admin_id=admin_id, rol=rol, device_id=device_id, **kw
            )


async def _filas(tid: uuid.UUID, admin_id: uuid.UUID | None = None):
    from core.db.models import AccountDevice
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            q = select(AccountDevice).where(AccountDevice.tenant_id == tid)
            if admin_id is not None:
                q = q.where(AccountDevice.admin_id == admin_id)
            return list((await session.execute(q)).scalars().all())


async def _tipos(tid: uuid.UUID) -> list[str]:
    from core.db.models import AccessEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(AccessEvent.tipo)
                    .where(AccessEvent.tenant_id == tid)
                    .order_by(AccessEvent.occurred_at)
                )
            ).scalars()
            return list(rows)


async def _poner_estado(tid: uuid.UUID, fila_id: uuid.UUID, estado: str) -> None:
    from core.db.models import AccountDevice
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(AccountDevice).where(AccountDevice.id == fila_id).values(estado=estado)
            )


# ------------------------------------------------------------------ servicio: decisión
async def test_abierto_registra_y_permite_sin_duplicar() -> None:
    tid, owner, _ = await _montar("ac-abierto")

    primera = await _evaluar(tid, owner, "owner", "EQUIPO-A", label="Pixel")
    segunda = await _evaluar(tid, owner, "owner", "EQUIPO-A")

    assert primera.permitido and primera.estado == "activo" and primera.politica == "abierto"
    assert segunda.permitido and segunda.device_row_id == primera.device_row_id
    filas = await _filas(tid, owner)
    assert len(filas) == 1
    assert filas[0].origen == "politica_abierta" and filas[0].rol_al_registrar == "owner"
    assert (await _tipos(tid)).count("dispositivo_nuevo") == 1


async def test_restringido_crea_pendiente_y_niega() -> None:
    tid, owner, _ = await _montar("ac-restr")

    primera = await _evaluar(tid, owner, "gestor", "EQUIPO-G")
    segunda = await _evaluar(tid, owner, "gestor", "EQUIPO-G")

    assert not primera.permitido and primera.code == "dispositivo_pendiente"
    assert not segunda.permitido and segunda.code == "dispositivo_pendiente"
    filas = await _filas(tid, owner)
    assert len(filas) == 1 and filas[0].estado == "pendiente" and filas[0].origen == "autoregistro"
    assert (await _tipos(tid)).count("dispositivo_pendiente") == 1


@pytest.mark.parametrize("rol", ["owner", "gestor"])
async def test_bloqueado_deniega_en_ambas_politicas(rol: str) -> None:
    """Regresión del hueco: un bloqueado jamás abre el acceso, ni con política abierta ni teniendo otro activo."""
    tid, owner, _ = await _montar(f"ac-bloq-{rol}")
    if rol == "owner":
        primera = await _evaluar(tid, owner, "owner", "EQUIPO-X")
    else:
        primera = await _evaluar(tid, owner, "gestor", "EQUIPO-X")
    await _poner_estado(tid, primera.device_row_id, "bloqueado")
    otro = await _evaluar(tid, owner, "owner", "EQUIPO-BUENO")  # otro activo no levanta el bloqueo

    d = await _evaluar(tid, owner, rol, "EQUIPO-X")

    assert otro.permitido
    assert not d.permitido and d.code == "dispositivo_bloqueado" and d.estado == "bloqueado"
    assert "dispositivo_bloqueado_usado" in await _tipos(tid)


async def test_pendiente_pasa_a_activo_si_la_politica_es_abierta() -> None:
    tid, owner, _ = await _montar("ac-promo")
    pend = await _evaluar(tid, owner, "gestor", "EQUIPO-P")  # restringido → pendiente
    assert pend.code == "dispositivo_pendiente"

    d = await _evaluar(tid, owner, "owner", "EQUIPO-P")  # misma cuenta, ahora como owner (abierto)

    assert d.permitido and d.estado == "activo"
    assert (await _filas(tid, owner))[0].estado == "activo"


async def test_sin_device_id_segun_politica() -> None:
    tid, owner, _ = await _montar("ac-sinid")

    restringido = await _evaluar(tid, owner, "gestor", None)
    abierto = await _evaluar(tid, owner, "owner", "  ")

    assert not restringido.permitido and restringido.code == "dispositivo_requerido"
    assert abierto.permitido and abierto.estado == "none"
    assert (await _tipos(tid)).count("dispositivo_sin_id") == 2
    assert await _filas(tid) == []


async def test_master_de_plataforma_no_se_registra_ni_se_bloquea() -> None:
    tid, owner, _ = await _montar("ac-master")

    d = await _evaluar(tid, owner, "owner", "EQUIPO-M", es_master_plataforma=True)

    assert d.permitido and d.politica == "plataforma"
    assert await _filas(tid) == []
    assert await _tipos(tid) == ["acceso_master"]


async def test_equipo_compartido_entre_cuentas_no_choca() -> None:
    tid, owner, _ = await _montar("ac-compart")
    colab, _ = await _colaborador(tid, uniq("ac-compart"))

    a = await _evaluar(tid, owner, "owner", "EQUIPO-COMUN")
    b = await _evaluar(tid, colab, "colaborador", "EQUIPO-COMUN")

    assert a.permitido and b.permitido and a.device_row_id != b.device_row_id
    assert len(await _filas(tid)) == 2
    assert "dispositivo_compartido" in await _tipos(tid)


async def test_altas_concurrentes_de_la_misma_cuenta_no_dan_500() -> None:
    tid, owner, _ = await _montar("ac-conc")

    resultados = await asyncio.gather(
        *(_evaluar(tid, owner, "owner", "EQUIPO-CONC") for _ in range(6)), return_exceptions=True
    )

    assert not [r for r in resultados if isinstance(r, Exception)], resultados
    assert all(r.permitido for r in resultados)
    assert len(await _filas(tid, owner)) == 1
    assert (await _tipos(tid)).count("dispositivo_nuevo") == 1


async def test_bloqueo_de_equipo_alcanza_a_todas_las_cuentas() -> None:
    from core.access import devices
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, owner, _ = await _montar("ac-bloqeq")
    colab, _ = await _colaborador(tid, uniq("ac-bloqeq"))
    a = await _evaluar(tid, owner, "owner", "EQUIPO-ROBADO")
    async with tenant_scope(tid):
        async with get_session() as session:
            await devices.bloquear(
                session,
                tenant_id=tid,
                device_row_id=a.device_row_id,
                esperado="activo",
                motivo="robado",
                alcance="equipo",
                actor_admin_id=owner,
                actor_es_owner_o_master=True,
            )

    d = await _evaluar(tid, colab, "colaborador", "EQUIPO-ROBADO")

    assert not d.permitido and d.code == "dispositivo_bloqueado"
    assert (
        await _filas(tid, colab) == []
    )  # no se le crea fila a una cuenta nueva en un equipo bloqueado


# ------------------------------------------------------------------ RLS
async def test_rls_aisla_las_filas_entre_negocios() -> None:
    from core.db.models import AccessEvent, AccountDevice
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid_a, owner_a, _ = await _montar("ac-rls-a")
    tid_b, _, _ = await _montar("ac-rls-b")
    await _evaluar(tid_a, owner_a, "owner", "EQUIPO-DE-A")

    async with tenant_scope(tid_b):
        async with get_session() as session:
            devs = (await session.execute(select(AccountDevice))).scalars().all()
            evs = (await session.execute(select(AccessEvent))).scalars().all()

    assert all(d.tenant_id == tid_b for d in devs)
    assert all(e.tenant_id == tid_b for e in evs)
    assert not [d for d in devs if d.device_id == "EQUIPO-DE-A"]


async def test_access_events_es_append_only() -> None:
    from sqlalchemy.exc import DBAPIError

    from core.db.models import AccessEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, owner, _ = await _montar("ac-append")
    await _evaluar(tid, owner, "owner", "EQUIPO-E")

    async with tenant_scope(tid):
        with pytest.raises(DBAPIError):
            async with get_session() as session:
                await session.execute(
                    update(AccessEvent)
                    .where(AccessEvent.tenant_id == tid)
                    .values(tipo="login_fallido")
                )
        with pytest.raises(DBAPIError):
            async with get_session() as session:
                await session.execute(
                    text("delete from access_events where tenant_id = :t"), {"t": tid}
                )
        async with get_session() as session:  # la baja lógica (retención) sí se permite
            await session.execute(
                update(AccessEvent)
                .where(AccessEvent.tenant_id == tid)
                .values(deleted_at=text("now()"))
            )


# ------------------------------------------------------------------ API: lecturas y aislamiento
async def test_lista_dispositivos_y_no_ve_los_de_otro_negocio(app_client) -> None:
    tid_a, owner_a, email_a = await _montar("ac-api-a")
    tid_b, owner_b, _ = await _montar("ac-api-b")
    await _evaluar(tid_a, owner_a, "owner", "EQUIPO-DE-A-12345")
    ajeno = await _evaluar(tid_b, owner_b, "owner", "EQUIPO-DE-B-67890")
    token = await _login(app_client, email_a)

    r = await app_client.get(f"{ACCESS}/devices", headers=_h(token, tid_a))
    con_cuenta_ajena = await app_client.get(
        f"{ACCESS}/devices", params={"admin_id": str(owner_b)}, headers=_h(token, tid_a)
    )
    detalle_ajeno = await app_client.get(
        f"{ACCESS}/devices/{ajeno.device_row_id}", headers=_h(token, tid_a)
    )

    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["device_id_corto"] == "-12345"
    assert "device_id" not in items[0]  # el listado enseña solo los últimos 6 caracteres
    assert items[0]["cuenta"]["email"] == email_a and items[0]["cuenta"]["rol"] == "owner"
    assert con_cuenta_ajena.json()["items"] == []
    assert detalle_ajeno.status_code == 404


async def test_detalle_muestra_el_identificador_completo(app_client) -> None:
    tid, owner, email = await _montar("ac-det")
    d = await _evaluar(tid, owner, "owner", "EQUIPO-COMPLETO-XYZ")
    token = await _login(app_client, email)

    r = await app_client.get(f"{ACCESS}/devices/{d.device_row_id}", headers=_h(token, tid))

    assert r.status_code == 200, r.text
    assert r.json()["device_id"] == "EQUIPO-COMPLETO-XYZ"


async def test_paginacion_por_cursor(app_client) -> None:
    tid, owner, email = await _montar("ac-pag")
    for n in range(3):
        await _evaluar(tid, owner, "owner", f"EQUIPO-PAG-{n}")
    token = await _login(app_client, email)

    p1 = await app_client.get(f"{ACCESS}/devices", params={"limit": 2}, headers=_h(token, tid))
    p2 = await app_client.get(
        f"{ACCESS}/devices",
        params={"limit": 2, "cursor": p1.json()["next_cursor"]},
        headers=_h(token, tid),
    )
    malo = await app_client.get(
        f"{ACCESS}/devices", params={"cursor": "no-es-un-cursor"}, headers=_h(token, tid)
    )

    assert len(p1.json()["items"]) == 2 and p1.json()["next_cursor"]
    assert len(p2.json()["items"]) == 1 and p2.json()["next_cursor"] is None
    assert malo.status_code == 400 and malo.json()["error"]["code"] == "cursor_invalido"


# ------------------------------------------------------------------ API: transiciones
async def test_aprobar_con_estado_esperado_y_409_si_cambio(app_client) -> None:
    tid, owner, email = await _montar("ac-aprob")
    d = await _evaluar(tid, owner, "gestor", "EQUIPO-PEND")
    token = await _login(app_client, email)
    url = f"{ACCESS}/devices/{d.device_row_id}/aprobar"

    ok = await app_client.post(url, json={"esperado": "pendiente"}, headers=_h(token, tid))
    otra_vez = await app_client.post(url, json={"esperado": "pendiente"}, headers=_h(token, tid))

    assert ok.status_code == 200, ok.text
    assert ok.json()["estado"] == "activo" and ok.json()["aprobado_at"]
    assert otra_vez.status_code == 409
    assert otra_vez.json()["error"]["code"] == "estado_desactualizado"
    assert otra_vez.json()["error"]["details"]["estado_actual"] == "activo"
    assert "dispositivo_aprobado" in await _tipos(tid)


async def test_bloquear_reactivar_y_no_dar_de_baja_un_bloqueado(app_client) -> None:
    tid, _, email = await _montar("ac-ciclo")
    colab, _ = await _colaborador(tid, uniq("ac-ciclo"))
    d = await _evaluar(tid, colab, "colaborador", "EQUIPO-CICLO")
    token = await _login(app_client, email)
    base = f"{ACCESS}/devices/{d.device_row_id}"

    bloq = await app_client.post(
        f"{base}/bloquear", json={"esperado": "activo", "motivo": "perdido"}, headers=_h(token, tid)
    )
    baja = await app_client.post(
        f"{base}/dar-de-baja", json={"esperado": "bloqueado"}, headers=_h(token, tid)
    )
    reactivar = await app_client.post(
        f"{base}/aprobar", json={"esperado": "bloqueado"}, headers=_h(token, tid)
    )

    assert (
        bloq.status_code == 200
        and bloq.json()["estado"] == "bloqueado"
        and bloq.json()["motivo"] == "perdido"
    )
    assert baja.status_code == 409 and baja.json()["error"]["code"] == "bloqueado_no_se_da_de_baja"
    assert reactivar.status_code == 200 and reactivar.json()["estado"] == "activo"


async def test_baja_es_logica_y_sale_de_la_lista(app_client) -> None:
    tid, owner, email = await _montar("ac-baja")
    d = await _evaluar(tid, owner, "gestor", "EQUIPO-BAJA")
    token = await _login(app_client, email)

    r = await app_client.post(
        f"{ACCESS}/devices/{d.device_row_id}/dar-de-baja",
        json={"esperado": "pendiente"},
        headers=_h(token, tid),
    )
    lista = await app_client.get(f"{ACCESS}/devices", headers=_h(token, tid))

    assert r.status_code == 204
    assert lista.json()["items"] == []
    filas = await _filas(tid)
    assert len(filas) == 1 and filas[0].deleted_at is not None  # sin borrado físico (invariante 8)


async def test_no_puedes_bloquear_el_equipo_desde_el_que_llamas(app_client) -> None:
    tid, owner, email = await _montar("ac-auto")
    d = await _evaluar(tid, owner, "owner", "MI-TELEFONO")
    token = await _login(app_client, email)

    r = await app_client.post(
        f"{ACCESS}/devices/{d.device_row_id}/bloquear",
        json={"esperado": "activo"},
        headers=_h(token, tid, device="MI-TELEFONO"),
    )

    assert r.status_code == 409 and r.json()["error"]["code"] == "no_puedes_bloquear_este_equipo"
    assert (await _filas(tid, owner))[0].estado == "activo"


async def test_alta_manual_y_cuenta_ajena(app_client) -> None:
    tid, owner, email = await _montar("ac-manual")
    ajeno, _ = await _colaborador((await _montar("ac-manual-b"))[0], uniq("ac-manual-b"))
    token = await _login(app_client, email)

    ok = await app_client.post(
        f"{ACCESS}/devices",
        json={"admin_id": str(owner), "device_id": "EQUIPO-MANUAL", "label": "Tablet"},
        headers=_h(token, tid),
    )
    repetido = await app_client.post(
        f"{ACCESS}/devices",
        json={"admin_id": str(owner), "device_id": "EQUIPO-MANUAL"},
        headers=_h(token, tid),
    )
    de_otro_negocio = await app_client.post(
        f"{ACCESS}/devices",
        json={"admin_id": str(ajeno), "device_id": "X-1"},
        headers=_h(token, tid),
    )

    assert ok.status_code == 201, ok.text
    assert ok.json()["estado"] == "activo" and ok.json()["origen"] == "alta_manual"
    assert (
        repetido.status_code == 409
        and repetido.json()["error"]["code"] == "dispositivo_ya_registrado"
    )
    assert de_otro_negocio.status_code == 404


# ------------------------------------------------------------------ API: permisos de sección
async def test_colaborador_sin_permiso_no_ve_dispositivos_ni_alertas(app_client) -> None:
    tid, _, _ = await _montar("ac-perm0")
    _, email = await _colaborador(tid, uniq("ac-perm0"))
    token = await _login(app_client, email)

    devices = await app_client.get(f"{ACCESS}/devices", headers=_h(token, tid))
    events = await app_client.get(f"{ACCESS}/events", headers=_h(token, tid))

    assert (
        devices.status_code == 403 and events.status_code == 403
    )  # opt-in: el dueño debe concederlo


async def test_colaborador_con_read_lista_pero_no_escribe(app_client) -> None:
    tid, owner, _ = await _montar("ac-perm1")
    _, email = await _colaborador(tid, uniq("ac-perm1"), {"devices": "read"})
    d = await _evaluar(tid, owner, "gestor", "EQUIPO-C1")
    token = await _login(app_client, email)

    lista = await app_client.get(f"{ACCESS}/devices", headers=_h(token, tid))
    aprobar = await app_client.post(
        f"{ACCESS}/devices/{d.device_row_id}/aprobar",
        json={"esperado": "pendiente"},
        headers=_h(token, tid),
    )

    assert lista.status_code == 200
    assert aprobar.status_code == 403


async def test_colaborador_con_write_no_toca_los_equipos_del_dueno(app_client) -> None:
    tid, owner, _ = await _montar("ac-perm2")
    colab, email = await _colaborador(tid, uniq("ac-perm2"), {"devices": "write"})
    del_dueno = await _evaluar(tid, owner, "owner", "EQUIPO-DUENO")
    propio = await _evaluar(tid, colab, "colaborador", "EQUIPO-COLAB")
    token = await _login(app_client, email)

    sobre_dueno = await app_client.post(
        f"{ACCESS}/devices/{del_dueno.device_row_id}/bloquear",
        json={"esperado": "activo"},
        headers=_h(token, tid),
    )
    sobre_propio = await app_client.post(
        f"{ACCESS}/devices/{propio.device_row_id}/bloquear",
        json={"esperado": "activo"},
        headers=_h(token, tid),
    )

    assert sobre_dueno.status_code == 403 and sobre_dueno.json()["error"]["code"] == "solo_dueno"
    assert sobre_propio.status_code == 200
    assert (await _filas(tid, owner))[0].estado == "activo"


# ------------------------------------------------------------------ API: eventos
async def test_feed_de_eventos_filtra_y_aisla(app_client) -> None:
    tid_a, owner_a, email_a = await _montar("ac-ev-a")
    tid_b, owner_b, _ = await _montar("ac-ev-b")
    await _evaluar(tid_a, owner_a, "gestor", "EQUIPO-EV")  # dispositivo_pendiente (aviso)
    await _evaluar(tid_a, owner_a, "owner", "EQUIPO-EV-2")  # dispositivo_nuevo (info)
    await _evaluar(tid_b, owner_b, "owner", "EQUIPO-DE-B")
    token = await _login(app_client, email_a)

    todos = await app_client.get(f"{ACCESS}/events", headers=_h(token, tid_a))
    avisos = await app_client.get(
        f"{ACCESS}/events", params={"severidad": "aviso"}, headers=_h(token, tid_a)
    )

    assert todos.status_code == 200, todos.text
    assert {e["tipo"] for e in todos.json()["items"]} == {
        "dispositivo_pendiente",
        "dispositivo_nuevo",
    }
    assert [e["tipo"] for e in avisos.json()["items"]] == ["dispositivo_pendiente"]
    assert all(e["cuenta"]["email"] == email_a for e in todos.json()["items"])


# ------------------------------------------------------------------ API: política
async def test_politica_por_defecto_y_cambio_del_dueno(app_client) -> None:
    from core.db.models import Tenant
    from core.db.session import get_session

    tid, _, email = await _montar("ac-pol")
    token = await _login(app_client, email)

    antes = await app_client.get(f"{ACCESS}/policy", headers=_h(token, tid))
    cambio = await app_client.put(
        f"{ACCESS}/policy", json={"colaborador": "restringido"}, headers=_h(token, tid)
    )
    despues = await app_client.get(f"{ACCESS}/policy", headers=_h(token, tid))

    assert antes.json()["owner"] == "abierto"
    assert antes.json()["colaborador"] == "abierto"
    assert antes.json()["gestor"] == "restringido"
    assert cambio.status_code == 200 and despues.json()["colaborador"] == "restringido"
    assert despues.json()["gestor"] == "restringido"  # lo no enviado no cambia
    async with get_session() as session:
        tenant = await session.get(Tenant, tid)
    assert tenant.settings["acceso"]["dispositivos"]["colaborador"] == "restringido"
    assert "politica_cambiada" in await _tipos(tid)


async def test_politica_la_cambia_solo_el_dueno(app_client) -> None:
    tid, _, _ = await _montar("ac-pol2")
    _, email = await _colaborador(tid, uniq("ac-pol2"), {"devices": "write"})
    token = await _login(app_client, email)

    r = await app_client.put(f"{ACCESS}/policy", json={"gestor": "abierto"}, headers=_h(token, tid))
    lectura = await app_client.get(f"{ACCESS}/policy", headers=_h(token, tid))

    assert r.status_code == 403
    assert lectura.status_code == 200  # leer la política es de cualquier cuenta del negocio


async def test_no_quedarse_fuera_exige_confirmar(app_client) -> None:
    tid, _, email = await _montar("ac-fuera")
    token = await _login(app_client, email)

    sin_confirmar = await app_client.put(
        f"{ACCESS}/policy", json={"owner": "restringido"}, headers=_h(token, tid)
    )
    confirmado = await app_client.put(
        f"{ACCESS}/policy", json={"owner": "restringido", "confirmar": True}, headers=_h(token, tid)
    )

    assert sin_confirmar.status_code == 409
    assert sin_confirmar.json()["error"]["code"] == "owners_sin_dispositivo_activo"
    assert sin_confirmar.json()["error"]["details"]["cuentas"][0]["email"] == email
    assert confirmado.status_code == 200 and confirmado.json()["owner"] == "restringido"


async def test_restringir_al_dueno_con_equipo_activo_no_pide_confirmar(app_client) -> None:
    tid, owner, email = await _montar("ac-fuera2")
    await _evaluar(tid, owner, "owner", "EQUIPO-DUENO")
    token = await _login(app_client, email)

    r = await app_client.put(
        f"{ACCESS}/policy", json={"owner": "restringido"}, headers=_h(token, tid)
    )

    assert r.status_code == 200, r.text


async def test_politica_rechaza_valores_invalidos(app_client) -> None:
    tid, _, email = await _montar("ac-pol3")
    token = await _login(app_client, email)

    r = await app_client.put(f"{ACCESS}/policy", json={"owner": "cerrado"}, headers=_h(token, tid))

    assert r.status_code == 422


# ------------------------------------------------------------------ escaladas dentro del negocio (auditoría)
async def test_colaborador_no_puede_bloquear_el_equipo_del_dueno_con_alcance_equipo(
    app_client,
) -> None:
    """Cadena: registrarse el equipo del dueño como propio y bloquearlo con alcance «equipo»."""
    tid, owner, _ = await _montar("ac-esc1")
    colab, email = await _colaborador(tid, uniq("ac-esc1"), {"devices": "write"})
    await _evaluar(tid, owner, "owner", "EQUIPO-DEL-DUENO")
    propio = await _evaluar(
        tid, colab, "colaborador", "EQUIPO-DEL-DUENO"
    )  # compartido: lo registra la política
    token = await _login(app_client, email)

    equipo = await app_client.post(
        f"{ACCESS}/devices/{propio.device_row_id}/bloquear",
        json={"esperado": "activo", "alcance": "equipo"},
        headers=_h(token, tid),
    )
    alta = await app_client.post(
        f"{ACCESS}/devices",
        json={"admin_id": str(colab), "device_id": "OTRO-EQUIPO"},
        headers=_h(token, tid),
    )

    assert equipo.status_code == 403 and equipo.json()["error"]["code"] == "solo_dueno"
    assert alta.status_code == 403 and alta.json()["error"]["code"] == "no_te_autorices"
    assert (
        await _evaluar(tid, owner, "owner", "EQUIPO-DEL-DUENO")
    ).permitido  # el dueño sigue entrando


async def test_colaborador_no_se_autoriza_su_propio_equipo(app_client) -> None:
    tid, _, _ = await _montar("ac-esc2")
    colab, email = await _colaborador(tid, uniq("ac-esc2"), {"devices": "write"})
    pend = await _evaluar(tid, colab, "gestor", "EQUIPO-PROPIO")  # restringido → pendiente
    token = await _login(app_client, email)

    r = await app_client.post(
        f"{ACCESS}/devices/{pend.device_row_id}/aprobar",
        json={"esperado": "pendiente"},
        headers=_h(token, tid),
    )

    assert r.status_code == 403 and r.json()["error"]["code"] == "no_te_autorices"
    assert (await _filas(tid, colab))[0].estado == "pendiente"


async def test_colaborador_no_levanta_un_bloqueo_del_dueno(app_client) -> None:
    tid, _, owner_email = await _montar("ac-esc3")
    _, colab_email = await _colaborador(tid, uniq("ac-esc3"), {"devices": "write"})
    otro, _ = await _colaborador(tid, uniq("ac-esc3b"))
    d = await _evaluar(tid, otro, "colaborador", "EQUIPO-OTRO")
    token_owner = await _login(app_client, owner_email)
    bloq = await app_client.post(
        f"{ACCESS}/devices/{d.device_row_id}/bloquear",
        json={"esperado": "activo", "motivo": "sospechoso"},
        headers=_h(token_owner, tid),
    )
    assert bloq.status_code == 200, bloq.text
    token_colab = await _login(app_client, colab_email)

    r = await app_client.post(
        f"{ACCESS}/devices/{d.device_row_id}/aprobar",
        json={"esperado": "bloqueado"},
        headers=_h(token_colab, tid),
    )
    el_dueno = await app_client.post(
        f"{ACCESS}/devices/{d.device_row_id}/aprobar",
        json={"esperado": "bloqueado"},
        headers=_h(token_owner, tid),
    )

    assert r.status_code == 403 and r.json()["error"]["code"] == "solo_dueno"
    assert el_dueno.status_code == 200 and el_dueno.json()["estado"] == "activo"


async def test_alta_manual_no_acepta_al_master_de_plataforma(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    tid, _, email = await _montar("ac-esc4")
    async with get_session() as session:
        master = Admin(
            email=f"{uniq('ac-esc4')}-m@test.local", password_hash=hash_password(PW), role="master"
        )
        session.add(master)
        await session.flush()
        master_id = master.id
    token = await _login(app_client, email)

    r = await app_client.post(
        f"{ACCESS}/devices",
        json={"admin_id": str(master_id), "device_id": "EQUIPO-M"},
        headers=_h(token, tid),
    )

    assert r.status_code == 404 or (
        r.status_code == 400 and r.json()["error"]["code"] == "master_no_se_registra"
    )
    assert await _filas(tid) == []


async def test_feed_no_muestra_la_cuenta_de_otro_negocio(app_client) -> None:
    """`access_events.admin_id` no tiene FK: una cuenta que no es del negocio no debe filtrar su email."""
    from core.access.events import registrar_evento
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid_a, _, email_a = await _montar("ac-feed-a")
    _, owner_b, email_b = await _montar("ac-feed-b")
    async with tenant_scope(tid_a):
        async with get_session() as session:
            registrar_evento(session, tenant_id=tid_a, tipo="login_fallido", admin_id=owner_b)
    token = await _login(app_client, email_a)

    r = await app_client.get(f"{ACCESS}/events", headers=_h(token, tid_a))

    assert r.status_code == 200, r.text
    assert [e["cuenta"] for e in r.json()["items"]] == [None]
    assert email_b not in r.text


async def test_device_id_completo_solo_a_quien_corresponde(app_client) -> None:
    tid, owner, owner_email = await _montar("ac-vis")
    lector, lector_email = await _colaborador(tid, uniq("ac-vis-r"), {"devices": "read"})
    _, admin_email = await _colaborador(tid, uniq("ac-vis-w"), {"devices": "write"})
    del_dueno = await _evaluar(tid, owner, "owner", "ID-COMPLETO-DUENO")
    de_colab = await _evaluar(tid, lector, "colaborador", "ID-COMPLETO-COLAB")

    async def detalle(email: str, fila_id: uuid.UUID) -> str | None:
        r = await app_client.get(
            f"{ACCESS}/devices/{fila_id}", headers=_h(await _login(app_client, email), tid)
        )
        assert r.status_code == 200, r.text
        return r.json()["device_id"]

    assert await detalle(owner_email, del_dueno.device_row_id) == "ID-COMPLETO-DUENO"
    assert (
        await detalle(lector_email, de_colab.device_row_id) is None
    )  # solo lectura: no ve el completo
    assert await detalle(admin_email, de_colab.device_row_id) == "ID-COMPLETO-COLAB"
    assert await detalle(admin_email, del_dueno.device_row_id) is None  # ni el del dueño


async def test_filtro_de_tipo_invalido_es_422(app_client) -> None:
    tid, _, email = await _montar("ac-tipo")
    token = await _login(app_client, email)

    r = await app_client.get(
        f"{ACCESS}/events", params={"tipo": "inventado"}, headers=_h(token, tid)
    )

    assert r.status_code == 422
