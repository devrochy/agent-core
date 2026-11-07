"""Un gestor no actúa sobre la cartera de otro gestor del MISMO negocio (agent-core#701).

RLS aísla negocios, pero dentro de un negocio los gestores comparten tenant: la API de campo debe
acotar cada acción a las carteras que el token puede ver. Cubre la cola offline (`carteraId`,
`prestamoId`, `clienteId` ajenos), eliminar pago/abono, `visita_no_pago`, las evidencias, el enlace
de la cuota y el escape del HTML de la liquidación. Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from _api_helpers import uniq
from test_pack_cobranza_permisos_gestor import (
    BASE,
    PW,
    TODOS,
    Escenario,
    _contar,
    _escenario,
    _fijar_permisos,
    _login,
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

_OTROS: dict[str, Otro] = {}


class Otro:
    """Segundo gestor del mismo negocio, con su cartera, cliente, préstamo, un pago y un abono."""

    def __init__(self) -> None:
        self.gestor_id: uuid.UUID
        self.cartera_id: uuid.UUID
        self.cliente_id: uuid.UUID
        self.prestamo_id: uuid.UUID
        self.cuota_ids: list[uuid.UUID] = []
        self.pago_id: uuid.UUID
        self.abono_id: uuid.UUID
        self.evidencia_id: uuid.UUID
        self.token = ""

    @property
    def ruta(self) -> str:
        return f"{BASE}/gestor/carteras/{self.cartera_id}"


async def _otro(app_client, esc: Escenario) -> Otro:
    from pack_cobranza.models import (
        Abono,
        Cartera,
        Cliente,
        ClienteEvidencia,
        Cuota,
        Gestor,
        GestorPermiso,
        Pago,
        Prestamo,
    )

    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    if esc.slug in _OTROS:
        return _OTROS[esc.slug]
    o = Otro()
    email = f"otro-{esc.slug}@test.local"
    async with get_session() as session:
        admin = Admin(email=email, username=f"o-{esc.slug}", password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        admin_id = admin.id
    tid = esc.tid
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(TenantUser(tenant_id=tid, admin_id=admin_id, role="colaborador"))
            g = Gestor(tenant_id=tid, admin_id=admin_id, nombre="Otro gestor")
            session.add(g)
            await session.flush()
            o.gestor_id = g.id
            session.add_all([GestorPermiso(tenant_id=tid, gestor_id=g.id, permiso=p, habilitado=True) for p in TODOS])
            cartera = Cartera(tenant_id=tid, nombre="Cartera del otro", gestor_id=g.id)
            session.add(cartera)
            await session.flush()
            o.cartera_id = cartera.id
            cli = Cliente(tenant_id=tid, cartera_id=cartera.id, nombre="Cliente del otro")
            session.add(cli)
            await session.flush()
            o.cliente_id = cli.id
            pr = Prestamo(
                tenant_id=tid, cliente_id=cli.id, cartera_id=cartera.id, valor=Decimal(100000),
                saldo=Decimal(130000), num_cuotas=13, dias_entre_cuotas=1, fecha_otorgado=date.today(),
            )  # fmt: skip
            session.add(pr)
            await session.flush()
            o.prestamo_id = pr.id
            for i in range(1, 14):
                c = Cuota(tenant_id=tid, prestamo_id=pr.id, numero_cuota=i, valor_esperado=Decimal(10000), fecha_vencimiento=date.today() + timedelta(days=i))
                session.add(c)
                await session.flush()
                o.cuota_ids.append(c.id)
            pago = Pago(tenant_id=tid, cliente_id=cli.id, prestamo_id=pr.id, valor=Decimal(1000), metodo_pago="efectivo", registrado_por="gestor")
            abono = Abono(tenant_id=tid, prestamo_id=pr.id, cliente_id=cli.id, valor=Decimal(1000), metodo_pago="efectivo", registrado_por="gestor")
            ev = ClienteEvidencia(tenant_id=tid, cliente_id=cli.id, tipo="foto_facial", ruta_archivo="no-existe.jpg", mimetype="image/jpeg")
            session.add_all([pago, abono, ev])
            await session.flush()
            o.pago_id, o.abono_id, o.evidencia_id = pago.id, abono.id, ev.id
    o.token = await _login(app_client, email, esc.slug)
    _OTROS[esc.slug] = o
    return o


async def _datos_del_otro(esc: Escenario, o: Otro) -> dict[str, float | int]:
    from pack_cobranza.models import Abono, Gasto, Pago, Promesa, Visita
    from pack_cobranza.models import Prestamo as P

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.tid):
        async with get_session() as session:
            saldo = float((await session.get(P, o.prestamo_id)).saldo)  # type: ignore[union-attr]
    return {
        "saldo": saldo,
        "pagos": await _contar(esc, Pago, prestamo_id=o.prestamo_id, deleted_at=None),
        "abonos": await _contar(esc, Abono, prestamo_id=o.prestamo_id, deleted_at=None),
        "visitas": await _contar(esc, Visita, cartera_id=o.cartera_id),
        "promesas": await _contar(esc, Promesa, prestamo_id=o.prestamo_id),
        "gastos": await _contar(esc, Gasto, cartera_id=o.cartera_id),
    }


async def _listos(app_client) -> tuple[Escenario, Otro]:
    esc = await _escenario(app_client, "aislados")
    o = await _otro(app_client, esc)
    await _fijar_permisos(esc, set(TODOS))
    return esc, o


# ------------------------------------------------------------------ cola offline
async def test_sync_offline_no_toca_la_cartera_de_otro_gestor(app_client) -> None:
    esc, o = await _listos(app_client)
    antes = await _datos_del_otro(esc, o)
    ajeno = {"carteraId": str(o.cartera_id), "prestamoId": str(o.prestamo_id), "clienteId": str(o.cliente_id)}
    eventos = [
        ("pago", {**_pago(esc), **ajeno, "cuotaId": str(o.cuota_ids[0])}),
        ("abono", {**_pago(esc, tipoPago="abono", valor=2000), **ajeno}),
        ("visita", {**_no_pago(esc), **ajeno}),
        ("gasto", {"carteraId": str(o.cartera_id), "descripcion": "ajeno", "valor": 1000}),
        ("trayectoria", {"carteraId": str(o.cartera_id), "puntos": []}),
        ("cambio_cliente", {"clienteId": str(o.cliente_id), "input": {"nombre": "x"}}),
        # cartera propia pero préstamo/cliente de la cartera del otro
        ("pago", {**_pago(esc), "carteraId": str(esc.cartera_id), "prestamoId": str(o.prestamo_id), "clienteId": str(o.cliente_id)}),
        ("pago", {**_pago(esc), "carteraId": str(esc.cartera_id), "prestamoId": str(o.prestamo_id)}),
    ]  # fmt: skip
    ids = [uniq(f"ev-{t}") for t, _ in eventos]

    r = await app_client.post(
        f"{BASE}/sync-offline/eventos",
        json={"eventos": [{"eventoIdCliente": i, "tipoEvento": t, "payload": d} for i, (t, d) in zip(ids, eventos, strict=True)]},
        headers=esc.h(),
    )

    assert r.status_code == 200, r.text
    assert [a["estado"] for a in r.json()] == ["error"] * len(eventos), r.text
    assert await _datos_del_otro(esc, o) == antes


async def test_sync_offline_sigue_aplicando_lo_propio(app_client) -> None:
    from pack_cobranza.models import Pago

    esc, _ = await _listos(app_client)
    antes = await _contar(esc, Pago, prestamo_id=esc.prestamo_id)
    r = await app_client.post(
        f"{BASE}/sync-offline/eventos",
        json={"eventos": [{"eventoIdCliente": uniq("ev-propio"), "tipoEvento": "pago", "payload": _pago(esc, carteraId=str(esc.cartera_id))}]},
        headers=esc.h(),
    )
    assert r.json()[0]["estado"] == "sincronizado", r.text
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == antes + 1


async def test_el_propietario_si_sincroniza_en_cualquier_cartera(app_client) -> None:
    esc, o = await _listos(app_client)
    r = await app_client.post(
        f"{BASE}/sync-offline/eventos",
        json={"eventos": [{"eventoIdCliente": uniq("ev-dueno"), "tipoEvento": "pago", "payload": {**_pago(esc), "carteraId": str(o.cartera_id), "prestamoId": str(o.prestamo_id), "clienteId": str(o.cliente_id), "cuotaId": str(o.cuota_ids[1])}}]},
        headers=esc.h(esc.token_owner),
    )
    assert r.json()[0]["estado"] == "sincronizado", r.text


# ------------------------------------------------------------------ eliminar pago / abono
async def test_no_elimina_pagos_ni_abonos_de_otra_cartera(app_client) -> None:
    esc, o = await _listos(app_client)
    antes = await _datos_del_otro(esc, o)
    for ruta, rid in (("pagos", o.pago_id), ("abonos", o.abono_id)):
        # por la cartera propia, con un movimiento ajeno
        r = await app_client.request("DELETE", f"{esc.ruta}/{ruta}/{rid}", json={"password": PW}, headers=esc.h())
        assert r.status_code == 404, (ruta, r.text)
        # por la cartera ajena
        r = await app_client.request("DELETE", f"{o.ruta}/{ruta}/{rid}", json={"password": PW}, headers=esc.h())
        assert r.status_code == 404, (ruta, r.text)
    assert await _datos_del_otro(esc, o) == antes


# ------------------------------------------------------------------ visita no pago
async def test_visita_no_pago_valida_prestamo_y_cliente(app_client) -> None:
    esc, o = await _listos(app_client)
    antes = await _datos_del_otro(esc, o)

    ajeno = {**_no_pago(esc), "prestamoId": str(o.prestamo_id), "clienteId": str(o.cliente_id)}
    r = await app_client.post(f"{esc.ruta}/visitas/no-pago", json=ajeno, headers=esc.h())
    assert r.status_code == 404, r.text

    sin_cliente = {k: v for k, v in _no_pago(esc).items() if k != "clienteId"}
    r = await app_client.post(f"{esc.ruta}/visitas/no-pago", json=sin_cliente, headers=esc.h())
    assert r.status_code == 400, r.text

    assert await _datos_del_otro(esc, o) == antes


# ------------------------------------------------------------------ evidencias
async def test_evidencia_solo_de_clientes_de_carteras_visibles(app_client) -> None:
    esc, o = await _listos(app_client)
    url = f"{BASE}/clientes/{o.cliente_id}/evidencia/{o.evidencia_id}"

    ajeno = await app_client.get(url, headers=esc.h())
    assert ajeno.status_code == 404 and ajeno.json()["error"] == "evidencia no encontrada", ajeno.text

    # El dueño de la evidencia y el propietario pasan la visibilidad (el archivo de prueba no existe).
    propio = await app_client.get(url, headers={"Authorization": f"Bearer {o.token}"})
    assert propio.status_code == 404 and propio.json()["error"] == "archivo no disponible", propio.text
    dueno = await app_client.get(url, headers=esc.h(esc.token_owner))
    assert dueno.status_code == 404 and dueno.json()["error"] == "archivo no disponible", dueno.text


# ------------------------------------------------------------------ cuota de otro préstamo
async def test_cuota_de_otro_prestamo_no_se_enlaza_ni_se_marca_pagada(app_client) -> None:
    from pack_cobranza.models import Cuota, Pago

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    esc, o = await _listos(app_client)
    ajena = o.cuota_ids[2]

    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=_pago(esc, cuotaId=str(ajena)), headers=esc.h())

    assert r.status_code == 201, r.text
    async with tenant_scope(esc.tid):
        async with get_session() as session:
            assert (await session.get(Cuota, ajena)).estatus == "pendiente"  # type: ignore[union-attr]
    assert await _contar(esc, Pago, cuota_id=ajena) == 0


# ------------------------------------------------------------------ HTML de la liquidación
async def test_export_pdf_escapa_el_nombre_de_la_cartera(app_client) -> None:
    from pack_cobranza.models import Cartera

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    esc, _ = await _listos(app_client)
    r = await app_client.post(f"{esc.ruta}/liquidaciones", json={}, headers=esc.h())
    assert r.status_code == 201, r.text
    liq_id = r.json()["id"]
    async with tenant_scope(esc.tid):
        async with get_session() as session:
            (await session.get(Cartera, esc.cartera_id)).nombre = "<script>alert(1)</script>"  # type: ignore[union-attr]

    r = await app_client.get(f"{esc.ruta}/liquidaciones/{liq_id}/export-pdf", headers=esc.h())

    assert r.status_code == 200, r.text
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text
