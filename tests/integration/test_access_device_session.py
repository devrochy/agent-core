"""`POST /access/device-session` y el equipo en la dependencia del principal (ADR-0174, HU-ACC-03).

Cubre el check-in del equipo por rol (dueño, colaborador, gestor, master), la política por rol (dueño
abierto frente a gestor restringido), el 403 con `code` cuando llega `X-Device-Id` de un equipo bloqueado o
pendiente, que sin header no se consulta nada (el panel web sigue igual), las rutas exentas, que Redis caído
no abre el acceso en restringido y que una ráfaga lee la base una sola vez. Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from test_access_dispositivos_core import (
    ACCESS,
    _colaborador,
    _filas,
    _h,
    _login,
    _tipos,
)
from test_access_dispositivos_core import _tenant as _tenant_core
from test_pack_cobranza_dispositivos_enforcement import _gestor, _login_admin
from test_pack_cobranza_dispositivos_enforcement import _tenant as _tenant_pack

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SESION = f"{ACCESS}/device-session"
LISTA = f"{ACCESS}/devices"
ME = "/api/v1/admin/me"


async def _politica(tenant_id: uuid.UUID, **valores: str) -> None:
    from core.access import policy
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await policy.escribir(session, tenant_id, valores)


async def _negocio(app_client, prefijo: str, *, con_pack: bool = False):
    """Negocio con dueño ya autenticado. Devuelve `(tid, owner_token, slug)`."""
    slug = uniq(prefijo)
    email = f"{slug}-owner@test.local"
    tid = await (_tenant_pack(email, slug) if con_pack else _tenant_core(email, slug))
    return tid, await _login(app_client, email), slug


async def _checkin(app_client, token: str, tid: uuid.UUID, equipo: str, **extra):
    return await app_client.post(
        SESION, json={"deviceId": equipo, **extra}, headers=_h(token, tid)
    )


async def test_el_dueno_y_el_colaborador_entran_con_la_politica_abierta(app_client) -> None:
    tid, owner_token, slug = await _negocio(app_client, "ds-roles")
    _, colab_email = await _colaborador(tid, slug)
    colab_token = await _login(app_client, colab_email)

    dueno = await _checkin(app_client, owner_token, tid, "EQ-DUENO", label="Moto G")
    colab = await _checkin(app_client, colab_token, tid, "EQ-COLAB")

    for r in (dueno, colab):
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo["permitido"] is True and cuerpo["estado"] == "activo"
        assert cuerpo["politica"] == "abierto" and cuerpo["requiereVinculacion"] is False
        assert cuerpo["dispositivoId"]
    assert {f.device_id for f in await _filas(tid)} == {"EQ-DUENO", "EQ-COLAB"}


async def test_el_gestor_queda_pendiente_con_la_politica_restringida(app_client) -> None:
    tid, _, slug = await _negocio(app_client, "ds-gestor", con_pack=True)
    gestor_email = f"{slug}-g@test.local"
    await _gestor(gestor_email, tid)
    token = await _login_admin(app_client, gestor_email)

    r = await _checkin(app_client, token, tid, "EQ-GESTOR")

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["permitido"] is False and cuerpo["estado"] == "pendiente"
    assert cuerpo["politica"] == "restringido" and cuerpo["requiereVinculacion"] is True


async def test_la_politica_se_aplica_por_rol_no_por_negocio(app_client) -> None:
    """Dueño en abierto y gestor en restringido conviven; si se restringe al dueño, su equipo nuevo espera."""
    tid, owner_token, slug = await _negocio(app_client, "ds-rol", con_pack=True)
    gestor_email = f"{slug}-g@test.local"
    await _gestor(gestor_email, tid)
    gestor_token = await _login_admin(app_client, gestor_email)

    dueno = await _checkin(app_client, owner_token, tid, "EQ-1")
    gestor = await _checkin(app_client, gestor_token, tid, "EQ-1")  # el mismo equipo, otra cuenta
    await _politica(tid, owner="restringido")
    dueno_nuevo = await _checkin(app_client, owner_token, tid, "EQ-2")

    assert dueno.json()["estado"] == "activo"
    assert gestor.json()["estado"] == "pendiente"
    assert dueno_nuevo.json()["estado"] == "pendiente"
    assert dueno_nuevo.json()["requiereVinculacion"] is True


async def test_el_master_no_se_registra_ni_se_bloquea(app_client) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    tid, _, _ = await _negocio(app_client, "ds-master")
    email = f"{uniq('ds-m')}@test.local"
    async with get_session() as session:
        session.add(Admin(email=email, password_hash=hash_password("password123"), role="master"))
    token = await _login(app_client, email)

    r = await _checkin(app_client, token, tid, "EQ-MASTER")

    assert r.status_code == 200, r.text
    assert r.json()["permitido"] is True and r.json()["politica"] == "plataforma"
    assert await _filas(tid) == []
    assert "acceso_master" in await _tipos(tid)


async def test_check_in_exige_identificador(app_client) -> None:
    tid, owner_token, _ = await _negocio(app_client, "ds-valida")

    vacio = await app_client.post(SESION, json={"deviceId": ""}, headers=_h(owner_token, tid))
    sin_cuerpo = await app_client.post(SESION, json={}, headers=_h(owner_token, tid))

    assert vacio.status_code == 422 and sin_cuerpo.status_code == 422


async def test_un_equipo_bloqueado_recibe_403_con_code_en_cada_peticion(app_client) -> None:
    tid, owner_token, _ = await _negocio(app_client, "ds-bloq")
    r = await _checkin(app_client, owner_token, tid, "EQ-B")
    fila_id = r.json()["dispositivoId"]
    antes = await app_client.get(LISTA, headers=_h(owner_token, tid, "EQ-B"))  # llena la caché

    # El bloqueo llega desde el panel (sin header) y corta la sesión del equipo al instante.
    bloqueo = await app_client.post(
        f"{LISTA}/{fila_id}/bloquear",
        json={"motivo": "perdido", "alcance": "cuenta", "esperado": "activo"},
        headers=_h(owner_token, tid),
    )
    despues = await app_client.get(LISTA, headers=_h(owner_token, tid, "EQ-B"))
    desde_el_panel = await app_client.get(LISTA, headers=_h(owner_token, tid))

    assert antes.status_code == 200
    assert bloqueo.status_code == 200, bloqueo.text
    assert despues.status_code == 403
    assert despues.json()["error"]["code"] == "dispositivo_bloqueado"
    assert desde_el_panel.status_code == 200  # el panel web no manda el header: no cambia nada


async def test_sin_header_no_se_consulta_el_equipo(app_client, monkeypatch) -> None:
    tid, owner_token, _ = await _negocio(app_client, "ds-sinhdr")

    async def _no_debe_llamarse(*_a, **_k):
        raise AssertionError("sin X-Device-Id no se evalúa el equipo")

    monkeypatch.setattr("core.access.sesion.evaluar_rapido", _no_debe_llamarse)

    r = await app_client.get(LISTA, headers=_h(owner_token, tid))

    assert r.status_code == 200, r.text


async def test_un_equipo_pendiente_solo_puede_consultar_su_estado(app_client) -> None:
    tid, owner_token, _ = await _negocio(app_client, "ds-exenta")
    await _politica(tid, owner="restringido")
    # Un equipo nuevo del dueño queda pendiente.
    r = await _checkin(app_client, owner_token, tid, "EQ-P")
    assert r.json()["estado"] == "pendiente"

    consulta = await _checkin(app_client, owner_token, tid, "EQ-P")
    yo = await app_client.get(ME, headers=_h(owner_token, tid, "EQ-P"))
    datos = await app_client.get(LISTA, headers=_h(owner_token, tid, "EQ-P"))
    edita = await app_client.patch(ME, json={}, headers=_h(owner_token, tid, "EQ-P"))

    assert consulta.status_code == 200 and consulta.json()["permitido"] is False
    assert yo.status_code == 200, yo.text  # `/admin/me` queda exenta: la app cierra sesión desde ahí
    assert datos.status_code == 403
    assert datos.json()["error"]["code"] == "dispositivo_pendiente"
    assert edita.status_code == 403  # la exención de `/admin/me` es solo para leer (GET)
    assert edita.json()["error"]["code"] == "dispositivo_pendiente"


async def test_redis_caido_en_restringido_no_abre_el_acceso(app_client, monkeypatch) -> None:
    tid, owner_token, _ = await _negocio(app_client, "ds-redis")
    await _politica(tid, owner="restringido")
    await _checkin(app_client, owner_token, tid, "EQ-NUEVO")  # pendiente
    # Un equipo activo del dueño, vía alta manual no hace falta: se aprueba el pendiente de otro.
    pendiente = (await _filas(tid))[0]

    class _RedisCaido:
        async def get(self, *_a, **_k):
            raise ConnectionError("redis caído")

        set = delete = get

    monkeypatch.setattr("core.access.sesion._redis", lambda: _RedisCaido())

    pasa_pendiente = await app_client.get(LISTA, headers=_h(owner_token, tid, "EQ-NUEVO"))
    pasa_sin_header = await app_client.get(LISTA, headers=_h(owner_token, tid))

    assert pendiente.estado == "pendiente"
    assert pasa_pendiente.status_code == 403
    assert pasa_pendiente.json()["error"]["code"] == "dispositivo_pendiente"
    assert pasa_sin_header.status_code == 200  # el panel web no depende del equipo ni de Redis


async def test_una_rafaga_lee_la_base_una_sola_vez(app_client, monkeypatch) -> None:
    from core.access import devices

    tid, owner_token, _ = await _negocio(app_client, "ds-rafaga")
    await _checkin(app_client, owner_token, tid, "EQ-R")
    original = devices._fila
    lecturas = 0

    async def _contada(*a, **k):
        nonlocal lecturas
        lecturas += 1
        return await original(*a, **k)

    monkeypatch.setattr(devices, "_fila", _contada)

    respuestas = [
        await app_client.get(LISTA, headers=_h(owner_token, tid, "EQ-R")) for _ in range(5)
    ]

    assert [r.status_code for r in respuestas] == [200] * 5
    assert lecturas == 1
