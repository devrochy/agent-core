"""Lo que quedó de los permisos y el aislamiento del gestor (agent-core#705, puntos 2 a 4).

Dos gestores del mismo negocio: la cola offline deduplica por cuenta, lo borrado o cerrado no admite
movimientos, las cuotas de suscripción se resuelven por su cartera, un gestor desactivado pierde la
sesión en el siguiente pedido y la API no entrega rutas del servidor. Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from _api_helpers import uniq
from test_pack_cobranza_gestores_aislados import Otro, _otro
from test_pack_cobranza_permisos_gestor import (
    BASE,
    PW,
    TODOS,
    Escenario,
    _contar,
    _escenario,
    _fijar_permisos,
    _no_pago,
    _pago,
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
    esc = await _escenario(app_client, "resto705")
    o = await _otro(app_client, esc)
    await _fijar_permisos(esc, set(TODOS))
    return esc, o


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _sync(app_client, token: str, eid: str, tipo: str, data: dict[str, Any]) -> dict[str, Any]:
    r = await app_client.post(
        f"{BASE}/sync-offline/eventos",
        json={"eventos": [{"eventoIdCliente": eid, "tipoEvento": tipo, "payload": data}]},
        headers=_h(token),
    )
    assert r.status_code == 200, r.text
    return r.json()[0]


async def _token_de_dispositivo(esc: Escenario) -> str:
    """Como el que emite `/auth/dispositivo`: sin cuenta, con el gestor del dispositivo."""
    from pack_cobranza.auth import issue_token, pack_secret

    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.tid):
        secret = await pack_secret()
    assert secret
    return issue_token({"tenant_id": str(esc.tid), "gestor_id": str(esc.gestor_id), "device_id": str(uuid.uuid4())}, secret)


async def _crear(esc: Escenario, *objs: Any) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.tid):
        async with get_session() as session:
            session.add_all(objs)


async def _prestamo(esc: Escenario, **campos: Any) -> uuid.UUID:
    from pack_cobranza.models import Cuota, Prestamo

    pid = uuid.uuid4()
    await _crear(
        esc,
        Prestamo(
            id=pid, tenant_id=esc.tid, cliente_id=esc.cliente_id, cartera_id=esc.cartera_id, valor=Decimal(50000),
            saldo=Decimal(50000), num_cuotas=1, dias_entre_cuotas=1, fecha_otorgado=date.today(), **campos,
        ),
        Cuota(tenant_id=esc.tid, prestamo_id=pid, numero_cuota=1, valor_esperado=Decimal(50000), fecha_vencimiento=date.today() + timedelta(days=1)),
    )  # fmt: skip
    return pid


async def _saldo_de(esc: Escenario, prestamo_id: uuid.UUID) -> float:
    from pack_cobranza.models import Prestamo

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.tid):
        async with get_session() as session:
            return float((await session.get(Prestamo, prestamo_id)).saldo)  # type: ignore[union-attr]


async def _cuota_de_suscripcion(esc: Escenario, *, cartera_id: uuid.UUID | None) -> uuid.UUID:
    from pack_cobranza.models import Cuota, Suscripcion

    sid, cid = uuid.uuid4(), uuid.uuid4()
    await _crear(
        esc,
        Suscripcion(id=sid, tenant_id=esc.tid, cliente_id=esc.cliente_id, cartera_id=cartera_id, concepto="Plan", valor_periodo=Decimal(20000)),
        Cuota(id=cid, tenant_id=esc.tid, suscripcion_id=sid, numero_cuota=1, valor_esperado=Decimal(20000), fecha_vencimiento=date.today() + timedelta(days=5)),
    )  # fmt: skip
    return cid


# ------------------------------------------------------------------ deduplicación offline
async def test_el_mismo_id_de_evento_de_dos_gestores_no_es_duplicado(app_client) -> None:
    from pack_cobranza.models import Gasto, SyncEvento

    esc, o = await _listos(app_client)
    eid = uniq("ev-compartido")
    propio = {"carteraId": str(esc.cartera_id), "descripcion": "Peaje", "valor": 1000}
    del_otro = {"carteraId": str(o.cartera_id), "descripcion": "Peaje", "valor": 1000}
    gastos_propios = await _contar(esc, Gasto, cartera_id=esc.cartera_id)
    gastos_del_otro = await _contar(esc, Gasto, cartera_id=o.cartera_id)

    assert (await _sync(app_client, esc.token_gestor, eid, "gasto", propio))["estado"] == "sincronizado"
    assert (await _sync(app_client, o.token, eid, "gasto", del_otro))["estado"] == "sincronizado"

    assert await _contar(esc, Gasto, cartera_id=esc.cartera_id) == gastos_propios + 1
    assert await _contar(esc, Gasto, cartera_id=o.cartera_id) == gastos_del_otro + 1
    assert await _contar(esc, SyncEvento, evento_id_cliente=eid) == 2

    # Reintentar sí es duplicado para cada uno y no vuelve a aplicar nada.
    assert (await _sync(app_client, esc.token_gestor, eid, "gasto", propio))["estado"] == "duplicado"
    assert (await _sync(app_client, o.token, eid, "gasto", del_otro))["estado"] == "duplicado"
    assert await _contar(esc, Gasto, cartera_id=esc.cartera_id) == gastos_propios + 1


async def test_dos_dispositivos_tampoco_comparten_el_id_de_evento(app_client) -> None:
    from pack_cobranza.models import Gasto

    esc, _ = await _listos(app_client)
    eid = uniq("ev-dispositivo")
    datos = {"carteraId": str(esc.cartera_id), "descripcion": "Peaje", "valor": 1000}
    gastos = await _contar(esc, Gasto, cartera_id=esc.cartera_id)
    uno, dos = await _token_de_dispositivo(esc), await _token_de_dispositivo(esc)

    assert (await _sync(app_client, uno, eid, "gasto", datos))["estado"] == "sincronizado"
    assert (await _sync(app_client, dos, eid, "gasto", datos))["estado"] == "sincronizado"
    assert (await _sync(app_client, uno, eid, "gasto", datos))["estado"] == "duplicado"
    assert await _contar(esc, Gasto, cartera_id=esc.cartera_id) == gastos + 2


async def test_un_evento_anterior_a_separar_por_cuenta_no_se_aplica_otra_vez(app_client) -> None:
    from pack_cobranza.models import Pago, SyncEvento

    esc, _ = await _listos(app_client)
    eid = uniq("ev-viejo")
    await _crear(esc, SyncEvento(tenant_id=esc.tid, evento_id_cliente=eid, tipo="pago", payload={}))
    pagos = await _contar(esc, Pago, prestamo_id=esc.prestamo_id)

    ack = await _sync(app_client, esc.token_gestor, eid, "pago", _pago(esc, carteraId=str(esc.cartera_id)))

    assert ack["estado"] == "duplicado"
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == pagos


async def test_sin_permiso_el_evento_no_se_guarda_aunque_la_cartera_no_resuelva(app_client) -> None:
    """El permiso va antes que la cartera en todos los tipos: lo rechazado por permiso es reintentable."""
    from pack_cobranza.models import SyncEvento

    esc, _ = await _listos(app_client)
    await _fijar_permisos(esc, set())
    eid = uniq("ev-sin-permiso")

    ack = await _sync(app_client, esc.token_gestor, eid, "pago", _pago(esc, carteraId=str(uuid.uuid4())))

    assert ack["estado"] == "error" and "registrar_pago" in ack["error"], ack
    assert await _contar(esc, SyncEvento, evento_id_cliente=eid) == 0


# ------------------------------------------------------------------ lo borrado o cerrado
@pytest.mark.parametrize(
    "campos",
    [{"estatus": "liquidado"}, {"estatus": "cancelado"}, {"deleted_at": datetime(2026, 1, 1, tzinfo=UTC)}],
    ids=["liquidado", "cancelado", "borrado"],
)
async def test_un_prestamo_cerrado_o_borrado_no_admite_visitas(app_client, campos) -> None:
    from pack_cobranza.models import Pago, Visita

    esc, _ = await _listos(app_client)
    pid = await _prestamo(esc, **campos)
    datos = {"prestamoId": str(pid), "clienteId": str(esc.cliente_id), "valor": 1000, "tipoPago": "cuota", "resultado": "pago", "metodoPago": "efectivo"}
    visitas = await _contar(esc, Visita, cartera_id=esc.cartera_id)

    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=datos, headers=esc.h())
    assert r.status_code == 404, r.text
    no_pago = {**_no_pago(esc), "prestamoId": str(pid)}
    r = await app_client.post(f"{esc.ruta}/visitas/no-pago", json=no_pago, headers=esc.h())
    assert r.status_code == 404, r.text
    ack = await _sync(app_client, esc.token_gestor, uniq("ev-cerrado"), "pago", {**datos, "carteraId": str(esc.cartera_id)})
    assert ack["estado"] == "error", ack

    assert await _contar(esc, Pago, prestamo_id=pid) == 0
    assert await _contar(esc, Visita, cartera_id=esc.cartera_id) == visitas
    assert await _saldo_de(esc, pid) == 50000.0


async def test_un_prestamo_vigente_sigue_admitiendo_visitas(app_client) -> None:
    esc, _ = await _listos(app_client)
    pid = await _prestamo(esc)
    datos = {"prestamoId": str(pid), "clienteId": str(esc.cliente_id), "valor": 1000, "tipoPago": "abono", "resultado": "pago", "metodoPago": "efectivo"}
    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=datos, headers=esc.h())
    assert r.status_code == 201, r.text
    assert await _saldo_de(esc, pid) == 49000.0


async def test_un_movimiento_ya_borrado_no_se_borra_otra_vez(app_client) -> None:
    from pack_cobranza.models import Abono, AuditoriaCartera, Pago

    esc, _ = await _listos(app_client)
    pago, abono = Pago(id=uuid.uuid4(), tenant_id=esc.tid, cliente_id=esc.cliente_id, prestamo_id=esc.prestamo_id, valor=Decimal(100), metodo_pago="efectivo", registrado_por="gestor"), Abono(id=uuid.uuid4(), tenant_id=esc.tid, cliente_id=esc.cliente_id, prestamo_id=esc.prestamo_id, valor=Decimal(100), metodo_pago="efectivo", registrado_por="gestor")  # fmt: skip
    await _crear(esc, pago, abono)
    for ruta, rid, entidad in (("pagos", pago.id, "pago"), ("abonos", abono.id, "abono")):
        url = f"{esc.ruta}/{ruta}/{rid}"
        assert (await app_client.request("DELETE", url, json={"password": PW}, headers=esc.h())).status_code == 200
        assert (await app_client.request("DELETE", url, json={"password": PW}, headers=esc.h())).status_code == 404
        assert await _contar(esc, AuditoriaCartera, entidad=entidad, entidad_id=rid, operacion="eliminar") == 1


async def test_lo_borrado_no_se_lee_ni_se_edita(app_client) -> None:
    esc, _ = await _listos(app_client)
    pid = await _prestamo(esc, deleted_at=datetime(2026, 1, 1, tzinfo=UTC))
    r = await app_client.get(f"{esc.ruta}/prestamos/{pid}/estado-cuenta", headers=esc.h())
    assert r.status_code == 404, r.text

    cuota = await _cuota_de_suscripcion(esc, cartera_id=esc.cartera_id)
    url = f"{esc.ruta}/cuotas/{cuota}"
    assert (await app_client.request("DELETE", url, json={"password": PW}, headers=esc.h())).status_code == 200
    assert (await app_client.request("DELETE", url, json={"password": PW}, headers=esc.h())).status_code == 404
    assert (await app_client.patch(url, json={"password": PW, "valorEsperado": 1}, headers=esc.h())).status_code == 404


# ------------------------------------------------------------------ cuotas de suscripción
@pytest.mark.parametrize("por", ["suscripcion", "cliente"])
async def test_cuota_de_suscripcion_se_opera_desde_su_cartera(app_client, por) -> None:
    from pack_cobranza.models import Cuota

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    esc, o = await _listos(app_client)
    cuota = await _cuota_de_suscripcion(esc, cartera_id=esc.cartera_id if por == "suscripcion" else None)
    url = f"{esc.ruta}/cuotas/{cuota}"

    # El otro gestor no la toca, ni por su cartera ni por la ajena.
    for ruta in (o.ruta, esc.ruta):
        r = await app_client.patch(f"{ruta}/cuotas/{cuota}", json={"password": PW, "valorEsperado": 1}, headers=_h(o.token))
        assert r.status_code == 404, r.text

    r = await app_client.patch(url, json={"password": PW, "valorEsperado": 25000}, headers=esc.h())
    assert r.status_code == 200, r.text
    async with tenant_scope(esc.tid):
        async with get_session() as session:
            assert float((await session.get(Cuota, cuota)).valor_esperado) == 25000.0  # type: ignore[union-attr]
    r = await app_client.request("DELETE", url, json={"password": PW}, headers=esc.h())
    assert r.status_code == 200, r.text


async def test_la_cuota_de_suscripcion_sigue_la_cartera_actual_del_cliente(app_client) -> None:
    """`Suscripcion.cartera_id` no se actualiza al mover al cliente: manda la cartera del cliente."""
    esc, o = await _listos(app_client)
    cuota = await _cuota_de_suscripcion(esc, cartera_id=o.cartera_id)  # el cliente ya está en la cartera de esc
    cuerpo = {"password": PW, "valorEsperado": 21000}
    r = await app_client.patch(f"{o.ruta}/cuotas/{cuota}", json=cuerpo, headers=_h(o.token))
    assert r.status_code == 404, r.text
    r = await app_client.patch(f"{esc.ruta}/cuotas/{cuota}", json=cuerpo, headers=esc.h())
    assert r.status_code == 200, r.text


async def test_el_link_de_una_cuota_de_suscripcion_lleva_el_cliente(app_client, monkeypatch) -> None:
    """Sin préstamo, el webhook solo puede registrar el pago con el cliente del link."""
    import pack_cobranza.pagos as pagos

    esc, o = await _listos(app_client)
    cuota = await _cuota_de_suscripcion(esc, cartera_id=esc.cartera_id)
    capturado: dict[str, Any] = {}

    async def _config():
        return SimpleNamespace(pago_qr_activo=True)

    async def _crear_link(c, cfg, *, cliente_id=None):
        capturado.update(cuota=c.id, cliente=cliente_id)
        return {"modo": "online"}

    monkeypatch.setattr(pagos, "config_del_tenant", _config)
    monkeypatch.setattr(pagos, "crear_link_para_cuota", _crear_link)

    r = await app_client.post(f"{esc.ruta}/cuotas/{cuota}/link-pago", headers=esc.h())
    assert r.status_code == 201, r.text
    assert capturado == {"cuota": cuota, "cliente": esc.cliente_id}
    r = await app_client.post(f"{o.ruta}/cuotas/{cuota}/link-pago", headers=_h(o.token))
    assert r.status_code == 404, r.text


# ------------------------------------------------------------------ sesión del gestor
@pytest.mark.parametrize(
    ("campo", "valor", "restaurar"),
    [("estatus", "inactivo", "activo"), ("deleted_at", datetime(2026, 1, 1, tzinfo=UTC), None)],
    ids=["desactivado", "borrado"],
)
async def test_el_gestor_desactivado_pierde_la_sesion_abierta(app_client, campo, valor, restaurar) -> None:
    from pack_cobranza.models import Gestor

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    esc, _ = await _listos(app_client)

    async def fijar(v: Any) -> None:
        async with tenant_scope(esc.tid):
            async with get_session() as session:
                gestor = await session.get(Gestor, esc.gestor_id)
                setattr(gestor, campo, v)

    dispositivo = await _token_de_dispositivo(esc)
    assert (await app_client.get(f"{BASE}/gestor/mis-carteras", headers=esc.h())).status_code == 200
    assert (await app_client.get(f"{BASE}/gestor/mis-carteras", headers=_h(dispositivo))).status_code == 200
    await fijar(valor)
    try:
        assert (await app_client.get(f"{BASE}/gestor/mis-carteras", headers=esc.h())).status_code == 401
        assert (await app_client.get(f"{BASE}/gestor/mis-carteras", headers=_h(dispositivo))).status_code == 401
        # El propietario del mismo negocio no se ve afectado.
        assert (await app_client.get(f"{BASE}/gestor/mis-carteras", headers=esc.h(esc.token_owner))).status_code == 200
    finally:
        await fijar(restaurar)
    assert (await app_client.get(f"{BASE}/gestor/mis-carteras", headers=esc.h())).status_code == 200


# ------------------------------------------------------------------ cartera borrada (decisión)
async def test_una_cartera_borrada_no_sirve_evidencias_ni_recibe_eventos(app_client) -> None:
    """Decisión documentada en agent-core#705: por la API de campo una cartera borrada ya no existe,
    tampoco para el propietario. Sus evidencias se siguen viendo en el panel."""
    from pack_cobranza.models import Cartera, Cliente, ClienteEvidencia, Prestamo

    esc, _ = await _listos(app_client)
    cid, clid, pid, evid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _crear(esc, Cartera(id=cid, tenant_id=esc.tid, nombre="Borrada", gestor_id=esc.gestor_id, deleted_at=datetime(2026, 1, 1, tzinfo=UTC)))
    await _crear(esc, Cliente(id=clid, tenant_id=esc.tid, cartera_id=cid, nombre="De la borrada"))
    await _crear(
        esc,
        Prestamo(id=pid, tenant_id=esc.tid, cliente_id=clid, cartera_id=cid, valor=Decimal(1000), saldo=Decimal(1000), num_cuotas=1, dias_entre_cuotas=1, fecha_otorgado=date.today()),
        ClienteEvidencia(id=evid, tenant_id=esc.tid, cliente_id=clid, tipo="foto_facial", ruta_archivo="x.jpg", mimetype="image/jpeg"),
    )  # fmt: skip

    r = await app_client.get(f"{BASE}/clientes/{clid}/evidencia/{evid}", headers=esc.h(esc.token_owner))
    assert r.status_code == 404 and r.json()["error"] == "evidencia no encontrada", r.text
    datos = {"carteraId": str(cid), "prestamoId": str(pid), "clienteId": str(clid), "valor": 100, "metodoPago": "efectivo"}
    ack = await _sync(app_client, esc.token_owner, uniq("ev-borrada"), "pago", datos)
    assert ack["estado"] == "error", ack


# ------------------------------------------------------------------ higiene
async def test_el_detalle_de_la_cuota_trae_la_ultima_visita_del_cliente(app_client) -> None:
    esc, _ = await _listos(app_client)
    url = f"{esc.ruta}/prestamos/{esc.prestamo_id}/cuotas/{esc.cuota_ids[0]}/detalle"
    r = await app_client.post(f"{esc.ruta}/visitas/no-pago", json={**_no_pago(esc), "motivoNoPago": "de viaje"}, headers=esc.h())
    assert r.status_code == 201, r.text

    r = await app_client.get(url, headers=esc.h())

    assert r.status_code == 200, r.text
    assert r.json()["ultimaVisita"]["motivoNoPago"] == "de viaje", r.text


async def test_el_gasto_publico_no_expone_la_ruta_del_servidor() -> None:
    from pack_cobranza import api_gestor

    gasto = SimpleNamespace(id=uuid.uuid4(), cartera_id=uuid.uuid4(), descripcion="Peaje", valor=Decimal(1), aprobado=False, aprobado_por=None, estado="pendiente", fecha_hora=None)  # fmt: skip
    evidencia = SimpleNamespace(id=uuid.uuid4(), gasto_id=gasto.id, nombre_original="a.jpg", mimetype="image/jpeg", tamano=1, ruta_archivo="/data/evidencias/secreto/a.jpg")  # fmt: skip

    publico = api_gestor._gasto_public(gasto, [evidencia])  # type: ignore[arg-type]

    assert "/data/evidencias" not in str(publico)
    assert "carteraArchivo" not in publico["evidencias"][0]
