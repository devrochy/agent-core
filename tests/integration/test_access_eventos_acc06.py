"""Eventos de acceso ampliados y límites de los equipos (ADR-0174, HU-ACC-06).

Cubre el `login_fallido`/`login_bloqueado` por negocio de la cuenta (fan-out con tope, dedupe de 5 min, sin
filas para identificadores que no son cuenta), el login v1 del pack, el lockout del login unificado de campo,
el tope de equipos por cuenta, la baja a pendiente al promover a gestor, la invalidación de caché al cambiar
la política y el rate limit de `device-session`. Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select
from test_access_dispositivos_core import ACCESS, _evaluar, _filas, _h, _login, _tipos
from test_access_dispositivos_core import _tenant as _tenant_core
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

LOGIN = "/api/v1/auth/login"
PACK = "/api/pack/pack_cobranza"
SESION = f"{ACCESS}/device-session"


@pytest.fixture(autouse=True)
async def _sin_bloqueos_de_login():
    """Los contadores de login (`auth:*`) y las ventanas de dedupe sobreviven entre tests: la IP de los
    tests es siempre la misma y cinco fallos la bloquearían para los demás."""

    async def limpiar() -> None:
        from core.memory.working import get_redis

        r = get_redis()
        for patron in ("auth:*", "acceso:dedupe:*", "acceso:device_session:*"):
            for clave in await r.keys(patron):
                await r.delete(clave)

    await limpiar()
    yield
    await limpiar()


async def _eventos(tid: uuid.UUID, tipo: str | None = None) -> list:
    from core.db.models import AccessEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            q = select(AccessEvent).where(AccessEvent.tenant_id == tid)
            if tipo:
                q = q.where(AccessEvent.tipo == tipo)
            return list((await session.execute(q.order_by(AccessEvent.occurred_at))).scalars().all())


async def _owner_id(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import TenantUser
    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(
                select(TenantUser.admin_id).where(
                    TenantUser.tenant_id == tid, TenantUser.role == "owner"
                )
            )
        ).scalar_one()


async def _miembro(tid: uuid.UUID, admin_id: uuid.UUID, rol: str = "colaborador") -> None:
    from core.db.models import TenantUser
    from core.db.session import get_session

    async with get_session() as session:
        session.add(TenantUser(tenant_id=tid, admin_id=admin_id, role=rol))


async def _negocio(prefijo: str, *, con_pack: bool = False) -> tuple[uuid.UUID, str, str]:
    slug = uniq(prefijo)
    email = f"{slug}@test.local"
    tid = await (_tenant_pack(email, slug) if con_pack else _tenant_core(email, slug))
    return tid, email, slug


async def _fallar(app_client, identificador: str):
    return await app_client.post(LOGIN, json={"identifier": identificador, "password": "incorrecta"})


# ------------------------------------------------------------------ login fallido
async def test_login_fallido_deja_un_evento_por_negocio_de_la_cuenta(app_client) -> None:
    tid_a, email, _ = await _negocio("ev-a")
    tid_b, _, _ = await _negocio("ev-b")
    await _miembro(tid_b, await _owner_id(tid_a))

    r = await _fallar(app_client, email)

    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    for tid in (tid_a, tid_b):
        [ev] = await _eventos(tid, "login_fallido")
        assert ev.severidad == "aviso" and ev.canal == "panel_apk" and ev.admin_id is not None
        assert "identificador" not in (ev.detalle or {})  # a veces ahí se escribe la contraseña


async def test_el_mismo_fallo_se_deduplica_y_la_siguiente_ventana_cuenta_las_repeticiones(
    app_client,
) -> None:
    from core.access.events import DEDUPE_S
    from core.memory.working import get_redis

    tid, email, _ = await _negocio("ev-dedupe")

    for _ in range(3):
        await _fallar(app_client, email)
    assert len(await _eventos(tid, "login_fallido")) == 1

    # Pasada la ventana (se simula vencida la clave) el siguiente fallo escribe y anota lo callado.
    r = get_redis()
    for clave in await r.keys("acceso:dedupe:*"):
        if not clave.endswith(":n"):
            await r.delete(clave)
    assert DEDUPE_S == 300
    await _fallar(app_client, email)
    eventos = await _eventos(tid, "login_fallido")
    assert len(eventos) == 2
    assert eventos[1].detalle == {"repeticiones_previas": 2}


async def test_un_identificador_que_no_es_cuenta_no_escribe_filas(app_client) -> None:
    tid, _, _ = await _negocio("ev-nadie")

    r = await _fallar(app_client, f"{uniq('fantasma')}@test.local")

    assert r.status_code == 401
    assert await _eventos(tid) == []


async def test_el_fanout_tiene_tope(app_client) -> None:
    from core.access.events import MAX_NEGOCIOS_POR_FALLO, registrar_fallo_de_login

    admin_id = None
    negocios = []
    for i in range(MAX_NEGOCIOS_POR_FALLO + 3):
        tid, _, _ = await _negocio(f"ev-tope{i}")
        negocios.append(tid)
        admin_id = admin_id or await _owner_id(negocios[0])
        if i:
            await _miembro(tid, admin_id)

    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        correo = (await session.execute(select(Admin.email).where(Admin.id == admin_id))).scalar_one()

    escritos = await registrar_fallo_de_login(correo, "10.0.0.1", canal="panel_apk")

    assert escritos == MAX_NEGOCIOS_POR_FALLO


async def test_si_el_fallo_dispara_el_bloqueo_queda_login_bloqueado(app_client) -> None:
    tid, email, _ = await _negocio("ev-bloq")

    for _ in range(5):
        r = await _fallar(app_client, email)
    assert r.status_code == 401  # el quinto fallo es el que bloquea; el sexto ya es 429

    [bloqueo] = await _eventos(tid, "login_bloqueado")
    assert bloqueo.severidad == "critico"
    assert (await _fallar(app_client, email)).status_code == 429


# ------------------------------------------------------------------ logins del pack
async def test_login_unificado_de_campo_tiene_lockout_y_deja_rastro(app_client) -> None:
    tid, email, _ = await _negocio("ev-campo", con_pack=True)
    cuerpo = {"usuario": email, "password": "incorrecta"}

    for _ in range(5):
        r = await app_client.post(f"{PACK}/auth/login", json=cuerpo)
        assert r.status_code == 401
    r = await app_client.post(f"{PACK}/auth/login", json=cuerpo)

    assert r.status_code == 429
    [fallo] = await _eventos(tid, "login_fallido")
    assert fallo.canal == "campo"
    assert len(await _eventos(tid, "login_bloqueado")) == 1


async def test_login_por_negocio_deja_rastro_solo_en_ese_negocio(app_client) -> None:
    tid, email, slug = await _negocio("ev-slug", con_pack=True)
    otro, _, _ = await _negocio("ev-slug-otro", con_pack=True)
    await _miembro(otro, await _owner_id(tid))

    r = await app_client.post(
        f"{PACK}/auth/propietario/login",
        json={"tenant": slug, "usuario": email, "password": "incorrecta"},
    )

    assert r.status_code == 401
    assert len(await _eventos(tid, "login_fallido")) == 1
    assert await _eventos(otro, "login_fallido") == []


async def test_login_unificado_con_tenant_solo_avisa_a_ese_negocio(app_client) -> None:
    tid, email, slug = await _negocio("ev-uni-tenant", con_pack=True)
    otro, _, _ = await _negocio("ev-uni-otro", con_pack=True)
    await _miembro(otro, await _owner_id(tid))

    r = await app_client.post(
        f"{PACK}/auth/login", json={"tenant": slug, "usuario": email, "password": "incorrecta"}
    )

    assert r.status_code == 401
    assert len(await _eventos(tid, "login_fallido")) == 1
    assert await _eventos(otro, "login_fallido") == []


async def test_login_v1_fallido_deja_evento_del_negocio_sin_cuenta(app_client) -> None:
    tid, _, slug = await _negocio("ev-v1", con_pack=True)

    for _ in range(3):
        r = await app_client.post(
            f"{PACK}/auth/dispositivo",
            json={"tenant": slug, "codigo": "DEV-XYZ", "api_key": "mala"},
        )
        assert r.status_code == 401

    [ev] = await _eventos(tid, "login_fallido")
    assert ev.canal == "campo_v1" and ev.admin_id is None and ev.detalle is None  # nada de lo que escribió


# ------------------------------------------------------------------ límites de equipos
async def test_una_cuenta_no_pasa_del_tope_de_equipos(app_client) -> None:
    from core.access.devices import MAX_EQUIPOS_POR_CUENTA, AccesoError

    tid, _, _ = await _negocio("ev-cap")
    owner = await _owner_id(tid)
    for i in range(MAX_EQUIPOS_POR_CUENTA):
        d = await _evaluar(tid, owner, "owner", f"EQ-{i}")
        assert d.permitido

    with pytest.raises(AccesoError) as exc:
        await _evaluar(tid, owner, "owner", "EQ-EXTRA")

    assert exc.value.status == 429 and exc.value.code == "demasiados_equipos"
    assert len(await _filas(tid, owner)) == MAX_EQUIPOS_POR_CUENTA
    # un equipo que ya estaba registrado sigue entrando aunque la cuenta esté en el tope
    assert (await _evaluar(tid, owner, "owner", "EQ-0")).permitido


async def test_device_session_tiene_rate_limit(app_client) -> None:
    tid, email, _ = await _negocio("ev-rate")
    token = await _login(app_client, email)

    codigos = [
        (await app_client.post(SESION, json={"deviceId": "EQ-1"}, headers=_h(token, tid))).status_code
        for _ in range(32)
    ]

    assert codigos[:30] == [200] * 30
    assert codigos[30:] == [429, 429]


async def test_al_volverse_gestor_sus_equipos_de_politica_abierta_bajan_a_pendiente(app_client) -> None:
    from core.access.devices import degradar_por_cambio_de_rol, invalidar_cuenta
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, _, slug = await _negocio("ev-promo", con_pack=True)
    gestor_email = f"{slug}-g@test.local"
    await _gestor(gestor_email, tid)  # crea la cuenta con su perfil Gestor
    from core.db.models import Admin

    async with get_session() as session:
        admin_id = (await session.execute(select(Admin.id).where(Admin.email == gestor_email))).scalar_one()
    # Entró como colaborador (política abierta) con un equipo, y otro lo aprobó una persona.
    await _evaluar(tid, admin_id, "colaborador", "EQ-ABIERTO")
    await _evaluar(
        tid, admin_id, "colaborador", "EQ-APROBADO", detalle_alta=None
    )
    filas = {f.device_id: f for f in await _filas(tid, admin_id)}
    from sqlalchemy import update

    from core.db.models import AccountDevice

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(AccountDevice).where(AccountDevice.id == filas["EQ-APROBADO"].id).values(origen="alta_manual")
            )

    async with tenant_scope(tid):
        async with get_session() as session:
            bajados = await degradar_por_cambio_de_rol(session, tid, admin_id, "gestor")
    await invalidar_cuenta(tid, admin_id, bajados)

    assert len(bajados) == 1
    estados = {f.device_id: f.estado for f in await _filas(tid, admin_id)}
    assert estados == {"EQ-ABIERTO": "pendiente", "EQ-APROBADO": "activo"}
    assert "dispositivo_pendiente" in await _tipos(tid)


async def test_cambiar_la_politica_invalida_la_cache_de_vigencia(app_client) -> None:
    from core.memory.working import get_redis

    tid, email, _ = await _negocio("ev-cache")
    token = await _login(app_client, email)
    await app_client.post(SESION, json={"deviceId": "EQ-1"}, headers=_h(token, tid))
    # Una petición con el header deja la decisión en caché 60 s.
    r = get_redis()
    await app_client.get(f"{ACCESS}/policy", headers=_h(token, tid, "EQ-1"))
    assert [c for c in await r.keys(f"acceso:req:{tid}:*")], "la decisión debía quedar en caché"

    resp = await app_client.put(f"{ACCESS}/policy", json={"colaborador": "restringido"}, headers=_h(token, tid))

    assert resp.status_code == 200, resp.text
    assert [c for c in await r.keys(f"acceso:req:{tid}:*")] == []


# ------------------------------------------------------------------ retención
async def test_la_retencion_da_de_baja_los_eventos_viejos_y_respeta_los_recientes(app_client) -> None:
    from datetime import UTC, datetime, timedelta

    from core.access.events import registrar_evento
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks.access_events_retention import RETENCION_DIAS, retener_eventos_de_acceso

    tid, _, _ = await _negocio("ev-ret")
    viejo = datetime.now(UTC) - timedelta(days=RETENCION_DIAS + 5)
    async with tenant_scope(tid):
        async with get_session() as session:
            registrar_evento(session, tenant_id=tid, tipo="login_fallido").occurred_at = viejo
            registrar_evento(session, tenant_id=tid, tipo="dispositivo_nuevo")

    resultado = await retener_eventos_de_acceso()

    assert resultado["dadas_de_baja"] >= 1
    vigentes = [e for e in await _eventos(tid) if e.deleted_at is None]
    assert [e.tipo for e in vigentes] == ["dispositivo_nuevo"]
    assert [e.tipo for e in await _eventos(tid) if e.deleted_at is not None] == ["login_fallido"]
