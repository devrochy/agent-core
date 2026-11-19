"""La API v1 de campo (`/sync/eventos`, `clientes-dia`, evidencia) aplica la matriz y las carteras (agent-core#690).

`POST /sync/eventos` acepta el mismo token de cuenta que la API v2: sin esto, un gestor sin
`registrar_pago` registraba el pago por aquí, en cualquier cartera del negocio. Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from test_pack_cobranza_gestores_aislados import Otro, _otro
from test_pack_cobranza_permisos_gestor import (
    BASE,
    TODOS,
    Escenario,
    _contar,
    _escenario,
    _fijar_permisos,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _listos(app_client) -> tuple[Escenario, Otro]:
    esc = await _escenario(app_client, "v1690")
    o = await _otro(app_client, esc)
    await _fijar_permisos(esc, set(TODOS))
    return esc, o


async def _sync(app_client, token: str, tipo: str, data: dict[str, Any], eid: str | None = None) -> dict[str, Any]:
    r = await app_client.post(
        f"{BASE}/sync/eventos",
        json={"eventos": [{"evento_id_cliente": eid or uniq("v1"), "tipo": tipo, "payload": data}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _pago_v1(esc: Escenario | Otro, **extra: Any) -> dict[str, Any]:
    return {"cliente_id": str(esc.cliente_id), "prestamo_id": str(esc.prestamo_id), "valor": 1000, "metodo_pago": "efectivo", **extra}


async def _token_de_dispositivo(esc: Escenario, *, gestor: bool = True) -> str:
    from pack_cobranza.auth import issue_token, pack_secret

    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.tid):
        secret = await pack_secret()
    assert secret
    gestor_id = str(esc.gestor_id) if gestor else None
    return issue_token({"tenant_id": str(esc.tid), "gestor_id": gestor_id, "device_id": str(uuid.uuid4())}, secret)


@pytest.mark.parametrize(
    ("tipo", "permiso", "modelo"),
    [("pago", "registrar_pago", "Pago"), ("abono", "registrar_abono", "Abono")],
)
async def test_sin_el_permiso_no_registra_y_se_puede_reintentar(app_client, tipo, permiso, modelo) -> None:
    import pack_cobranza.models as m

    esc, _ = await _listos(app_client)
    tabla = getattr(m, modelo)
    await _fijar_permisos(esc, set(TODOS) - {permiso})
    antes = await _contar(esc, tabla, prestamo_id=esc.prestamo_id)
    eid = uniq("v1-permiso")

    out = await _sync(app_client, esc.token_gestor, tipo, _pago_v1(esc), eid)

    assert out["aplicados"] == 0 and out["rechazados"] == 1, out
    assert await _contar(esc, tabla, prestamo_id=esc.prestamo_id) == antes
    assert await _contar(esc, m.SyncEvento, evento_id_cliente=eid) == 0

    await _fijar_permisos(esc, set(TODOS))
    out = await _sync(app_client, esc.token_gestor, tipo, _pago_v1(esc), eid)
    assert out["aplicados"] == 1, out
    assert await _contar(esc, tabla, prestamo_id=esc.prestamo_id) == antes + 1


async def test_una_visita_exige_el_permiso_de_su_resultado(app_client) -> None:
    from pack_cobranza.models import Visita

    esc, _ = await _listos(app_client)
    await _fijar_permisos(esc, set(TODOS) - {"registrar_no_pago"})
    antes = await _contar(esc, Visita, cartera_id=esc.cartera_id)

    out = await _sync(app_client, esc.token_gestor, "visita", {"cliente_id": str(esc.cliente_id), "resultado": "no_pago"})

    assert out["rechazados"] == 1, out
    assert await _contar(esc, Visita, cartera_id=esc.cartera_id) == antes


async def test_el_token_de_dispositivo_del_gestor_tambien_aplica_la_matriz(app_client) -> None:
    from pack_cobranza.models import Pago

    esc, _ = await _listos(app_client)
    await _fijar_permisos(esc, set())
    antes = await _contar(esc, Pago, prestamo_id=esc.prestamo_id)

    out = await _sync(app_client, await _token_de_dispositivo(esc), "pago", _pago_v1(esc))

    assert out["rechazados"] == 1, out
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == antes


async def test_no_registra_en_la_cartera_de_otro_gestor(app_client) -> None:
    from pack_cobranza.models import Abono, Pago, Visita

    esc, o = await _listos(app_client)
    antes = [await _contar(esc, Pago, prestamo_id=o.prestamo_id), await _contar(esc, Abono, prestamo_id=o.prestamo_id), await _contar(esc, Visita, cartera_id=o.cartera_id)]

    for tipo, data in (("pago", _pago_v1(o)), ("abono", _pago_v1(o)), ("visita", {"cliente_id": str(o.cliente_id)})):
        out = await _sync(app_client, esc.token_gestor, tipo, data)
        assert out["aplicados"] == 0, (tipo, out)

    assert antes == [await _contar(esc, Pago, prestamo_id=o.prestamo_id), await _contar(esc, Abono, prestamo_id=o.prestamo_id), await _contar(esc, Visita, cartera_id=o.cartera_id)]


async def test_no_cuelga_el_pago_de_un_prestamo_o_cuota_ajenos(app_client) -> None:
    from pack_cobranza.models import Cuota, Pago

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    esc, o = await _listos(app_client)
    antes = await _contar(esc, Pago, prestamo_id=o.prestamo_id)

    out = await _sync(app_client, esc.token_gestor, "pago", {**_pago_v1(esc), "prestamo_id": str(o.prestamo_id)})
    assert out["aplicados"] == 0, out
    assert await _contar(esc, Pago, prestamo_id=o.prestamo_id) == antes

    out = await _sync(app_client, esc.token_gestor, "pago", _pago_v1(esc, cuota_id=str(o.cuota_ids[0])))
    assert out["aplicados"] == 1, out
    async with tenant_scope(esc.tid):
        async with get_session() as session:
            assert (await session.get(Cuota, o.cuota_ids[0])).estatus == "pendiente"  # type: ignore[union-attr]


async def test_el_propietario_y_el_dispositivo_sin_gestor_siguen_igual(app_client) -> None:
    from pack_cobranza.models import Pago

    esc, o = await _listos(app_client)
    antes = await _contar(esc, Pago, prestamo_id=o.prestamo_id)

    assert (await _sync(app_client, esc.token_owner, "pago", _pago_v1(o)))["aplicados"] == 1
    assert (await _sync(app_client, await _token_de_dispositivo(esc, gestor=False), "pago", _pago_v1(o)))["aplicados"] == 1
    assert await _contar(esc, Pago, prestamo_id=o.prestamo_id) == antes + 2


async def test_clientes_dia_solo_de_carteras_visibles(app_client) -> None:
    esc, o = await _listos(app_client)
    h = esc.h()
    r = await app_client.get(f"{BASE}/carteras/{o.cartera_id}/clientes-dia", headers=h)
    assert r.status_code == 404, r.text
    r = await app_client.get(f"{BASE}/carteras/{esc.cartera_id}/clientes-dia", headers=h)
    assert r.status_code == 200, r.text
    r = await app_client.get(f"{BASE}/carteras/{o.cartera_id}/clientes-dia", headers=esc.h(esc.token_owner))
    assert r.status_code == 200, r.text


async def test_evidencia_exige_cartera_visible_y_permiso(app_client) -> None:
    esc, o = await _listos(app_client)
    archivo = {"archivo": ("a.jpg", b"x", "image/jpeg")}
    # `tipo` inválido: si pasa los controles responde 400, sin escribir ningún archivo.
    datos = {"tipo": "no-existe"}

    r = await app_client.post(f"{BASE}/clientes/{o.cliente_id}/evidencia", data=datos, files=archivo, headers=esc.h())
    assert r.status_code == 404, r.text
    await _fijar_permisos(esc, set(TODOS) - {"actualizar_cliente"})
    r = await app_client.post(f"{BASE}/clientes/{esc.cliente_id}/evidencia", data=datos, files=archivo, headers=esc.h())
    assert r.status_code == 403 and r.json()["permiso"] == "actualizar_cliente", r.text
    await _fijar_permisos(esc, set(TODOS))
    r = await app_client.post(f"{BASE}/clientes/{esc.cliente_id}/evidencia", data=datos, files=archivo, headers=esc.h())
    assert r.status_code == 400, r.text
