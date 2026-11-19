"""La API de campo aplica la matriz de permisos del gestor en el servidor (agent-core#690).

Antes la matriz (`pack_cobranza_gestor_permisos`) solo se le devolvía a la app: quitar
`registrar_pago` no impedía registrar el pago a quien llamara la API. Cubre cada acción de
la matriz que tiene ruta (pago, abono, no pago + promesa, gasto, préstamo, nota, cliente,
eliminar pago/abono, liquidación y la cola offline), el caso feliz con el permiso, el
propietario sin cambios y el aislamiento entre negocios. Requiere el stack local.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

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

BASE = "/api/pack/pack_cobranza"
PW = "password123"
TODOS = (
    "registrar_prestamo", "registrar_pago", "registrar_abono", "registrar_gasto",
    "registrar_no_pago", "anotar_notas_cartera", "actualizar_cliente", "eliminar_prestamo",
    "eliminar_pago", "eliminar_abono", "eliminar_gasto", "registrar_inyeccion",
    "ver_cartera", "generar_reporte",
)  # fmt: skip

_ESCENARIOS: dict[str, Escenario] = {}


class Escenario:
    """Un negocio de cobranza con propietario, un gestor con cartera, un cliente y un préstamo."""

    def __init__(self, slug: str, tid: uuid.UUID) -> None:
        self.slug = slug
        self.tid = tid
        self.owner_email = f"{slug}@test.local"
        self.gestor_email = f"gestor-{slug}@test.local"
        self.gestor_id: uuid.UUID
        self.cartera_id: uuid.UUID
        self.cliente_id: uuid.UUID
        self.prestamo_id: uuid.UUID
        self.cuota_ids: list[uuid.UUID] = []
        self.token_gestor = ""
        self.token_owner = ""

    def h(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token_gestor}"}

    @property
    def ruta(self) -> str:
        return f"{BASE}/gestor/carteras/{self.cartera_id}"


async def _montar(app_client, nombre: str) -> Escenario:
    from datetime import date, timedelta
    from decimal import Decimal

    from pack_cobranza.models import Cartera, Cliente, Cuota, Gestor, Prestamo

    from core.access import policy
    from core.admin.auth import hash_password
    from core.alta_negocio import onboard_tenant
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq(nombre)
    tid, _ = await onboard_tenant(
        name=nombre,
        slug=slug,
        business_description="Permisos del gestor.",
        persona={"tone": "cercano"},
        admin_email=f"{slug}@test.local",
        admin_password=PW,
        packs=["pack_cobranza"],
    )
    esc = Escenario(slug, tid)
    async with get_session() as session:
        admin = Admin(email=esc.gestor_email, username=f"g-{slug}", password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        admin_id = admin.id
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(TenantUser(tenant_id=tid, admin_id=admin_id, role="colaborador"))
            gestor = Gestor(tenant_id=tid, admin_id=admin_id, nombre="Gestor Permisos")
            session.add(gestor)
            await session.flush()
            esc.gestor_id = gestor.id
            cartera = Cartera(tenant_id=tid, nombre="Cartera P", gestor_id=gestor.id)
            session.add(cartera)
            await session.flush()
            esc.cartera_id = cartera.id
            cliente = Cliente(tenant_id=tid, cartera_id=cartera.id, nombre="Cliente P")
            session.add(cliente)
            await session.flush()
            esc.cliente_id = cliente.id
            prestamo = Prestamo(
                tenant_id=tid, cliente_id=cliente.id, cartera_id=cartera.id, valor=Decimal(100000),
                saldo=Decimal(130000), num_cuotas=13, dias_entre_cuotas=1, fecha_otorgado=date.today(),
            )  # fmt: skip
            session.add(prestamo)
            await session.flush()
            esc.prestamo_id = prestamo.id
            for i in range(1, 14):
                c = Cuota(
                    tenant_id=tid, prestamo_id=prestamo.id, numero_cuota=i,
                    valor_esperado=Decimal(10000), fecha_vencimiento=date.today() + timedelta(days=i),
                )  # fmt: skip
                session.add(c)
                await session.flush()
                esc.cuota_ids.append(c.id)
        async with get_session() as session:
            # El gestor entra sin aprobación de equipo: este archivo es de permisos, no de dispositivos.
            await policy.escribir(session, tid, {"gestor": "abierto"})
    esc.token_gestor = await _login(app_client, esc.gestor_email, slug)
    esc.token_owner = await _login(app_client, esc.owner_email, None)
    return esc


async def _login(app_client, usuario: str, tenant: str | None) -> str:
    body: dict[str, Any] = {"usuario": usuario, "password": PW, "imei": "IMEI-PERMISOS"}
    if tenant is not None:
        body["tenant"] = tenant
    r = await app_client.post(f"{BASE}/auth/login", json=body)
    assert r.status_code == 200, r.text
    return r.json()["accessToken"]


async def _escenario(app_client, clave: str = "a") -> Escenario:
    if clave not in _ESCENARIOS:
        _ESCENARIOS[clave] = await _montar(app_client, f"perm-{clave}")
    return _ESCENARIOS[clave]


async def _fijar_permisos(esc: Escenario, habilitados: tuple[str, ...] | set[str]) -> None:
    """Deja la matriz del gestor exactamente en `habilitados` (el resto, deshabilitado)."""
    from pack_cobranza.models import GestorPermiso
    from sqlalchemy import delete

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.tid):
        async with get_session() as session:
            await session.execute(
                delete(GestorPermiso).where(GestorPermiso.tenant_id == esc.tid, GestorPermiso.gestor_id == esc.gestor_id)
            )
            for p in TODOS:
                session.add(GestorPermiso(tenant_id=esc.tid, gestor_id=esc.gestor_id, permiso=p, habilitado=p in habilitados))


async def _contar(esc: Escenario, modelo: Any, **filtros: Any) -> int:
    from sqlalchemy import func, select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.tid):
        async with get_session() as session:
            q = select(func.count()).select_from(modelo).where(modelo.tenant_id == esc.tid)
            for k, v in filtros.items():
                q = q.where(getattr(modelo, k) == v)
            return int((await session.execute(q)).scalar_one())


async def _saldo(esc: Escenario) -> float:
    from pack_cobranza.models import Prestamo

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(esc.tid):
        async with get_session() as session:
            return float((await session.get(Prestamo, esc.prestamo_id)).saldo)  # type: ignore[union-attr]


def _pago(esc: Escenario, **extra: Any) -> dict[str, Any]:
    return {
        "resultado": "pago", "tipoPago": "cuota", "valor": 10000, "metodoPago": "efectivo",
        "prestamoId": str(esc.prestamo_id), "clienteId": str(esc.cliente_id),
        "cuotaId": str(esc.cuota_ids[13 - 1]), **extra,
    }  # fmt: skip


def _no_pago(esc: Escenario, **extra: Any) -> dict[str, Any]:
    return {
        "prestamoId": str(esc.prestamo_id), "clienteId": str(esc.cliente_id), "motivoNoPago": "no estaba",
        "fechaPrometida": "2030-01-01", "valorPrometido": 5000, **extra,
    }  # fmt: skip


# ------------------------------------------------------------------ registrar pago (el caso del issue)
async def test_sin_registrar_pago_responde_403_y_no_escribe(app_client) -> None:
    from pack_cobranza.models import Abono, Pago, Visita

    esc = await _escenario(app_client)
    await _fijar_permisos(esc, set(TODOS) - {"registrar_pago"})
    saldo = await _saldo(esc)

    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=_pago(esc), headers=esc.h())

    assert r.status_code == 403, r.text
    assert r.json()["permiso"] == "registrar_pago"
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == 0
    assert await _contar(esc, Abono, prestamo_id=esc.prestamo_id) == 0
    assert await _contar(esc, Visita, cartera_id=esc.cartera_id) == 0
    assert await _saldo(esc) == saldo


async def test_con_registrar_pago_lo_registra(app_client) -> None:
    from pack_cobranza.models import Pago

    esc = await _escenario(app_client)
    await _fijar_permisos(esc, {"registrar_pago"})
    saldo = await _saldo(esc)

    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=_pago(esc), headers=esc.h())

    assert r.status_code == 201, r.text
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) >= 1
    assert await _saldo(esc) == saldo - 10000


async def test_registrar_abono_exige_su_propio_permiso(app_client) -> None:
    from pack_cobranza.models import Abono

    esc = await _escenario(app_client)
    abono = _pago(esc, tipoPago="abono", valor=2000)
    abono.pop("cuotaId")
    await _fijar_permisos(esc, {"registrar_pago"})  # tener registrar_pago NO habilita abonos
    antes = await _contar(esc, Abono, prestamo_id=esc.prestamo_id)

    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=abono, headers=esc.h())
    assert r.status_code == 403, r.text
    assert r.json()["permiso"] == "registrar_abono"
    assert await _contar(esc, Abono, prestamo_id=esc.prestamo_id) == antes

    await _fijar_permisos(esc, {"registrar_abono"})
    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=abono, headers=esc.h())
    assert r.status_code == 201, r.text
    assert await _contar(esc, Abono, prestamo_id=esc.prestamo_id) == antes + 1


async def test_visitas_pago_sin_resultado_exige_registrar_no_pago(app_client) -> None:
    """`/visitas/pago` sin `resultado` se aplica como NO pago (y crea una promesa): el permiso
    sale de la acción efectiva, no de la URL."""
    from pack_cobranza.models import Promesa, Visita

    esc = await _escenario(app_client)
    cuerpo = _no_pago(esc)  # sin "resultado"
    await _fijar_permisos(esc, set(TODOS) - {"registrar_no_pago"})
    visitas = await _contar(esc, Visita, cartera_id=esc.cartera_id)
    promesas = await _contar(esc, Promesa, prestamo_id=esc.prestamo_id)

    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=cuerpo, headers=esc.h())

    assert r.status_code == 403, r.text
    assert r.json()["permiso"] == "registrar_no_pago"
    assert await _contar(esc, Visita, cartera_id=esc.cartera_id) == visitas
    assert await _contar(esc, Promesa, prestamo_id=esc.prestamo_id) == promesas


# ------------------------------------------------------------------ no pago, promesa
async def test_no_pago_y_promesa_exigen_registrar_no_pago(app_client) -> None:
    from pack_cobranza.models import Promesa, Visita

    esc = await _escenario(app_client)
    await _fijar_permisos(esc, set(TODOS) - {"registrar_no_pago"})
    visitas = await _contar(esc, Visita, cartera_id=esc.cartera_id)
    promesas = await _contar(esc, Promesa, prestamo_id=esc.prestamo_id)

    r = await app_client.post(f"{esc.ruta}/visitas/no-pago", json=_no_pago(esc), headers=esc.h())
    assert r.status_code == 403, r.text
    assert r.json()["permiso"] == "registrar_no_pago"
    assert await _contar(esc, Visita, cartera_id=esc.cartera_id) == visitas
    assert await _contar(esc, Promesa, prestamo_id=esc.prestamo_id) == promesas

    await _fijar_permisos(esc, {"registrar_no_pago"})
    r = await app_client.post(f"{esc.ruta}/visitas/no-pago", json=_no_pago(esc), headers=esc.h())
    assert r.status_code == 201, r.text
    assert await _contar(esc, Visita, cartera_id=esc.cartera_id) == visitas + 1
    assert await _contar(esc, Promesa, prestamo_id=esc.prestamo_id) == promesas + 1


# ------------------------------------------------------------------ gasto, préstamo, nota, cliente
async def test_gasto_prestamo_nota_y_cliente_exigen_su_permiso(app_client) -> None:
    from pack_cobranza.models import CarteraNota, Gasto, Prestamo

    esc = await _escenario(app_client)
    gasto = {"descripcion": "Gasolina", "valor": "5000"}
    prestamo = {"clienteId": str(esc.cliente_id), "valor": 50000, "numCuotas": 5, "diasEntreCuotas": 1}
    nota = {"nota": "Pasar el viernes"}
    ubicacion = {"latitud": 5.07, "longitud": -75.5}
    casos = [
        ("registrar_gasto", "post", f"{esc.ruta}/gastos", {"data": gasto}, 201, (Gasto, {"cartera_id": esc.cartera_id})),
        ("registrar_prestamo", "post", f"{esc.ruta}/prestamos", {"json": prestamo}, 201, (Prestamo, {"cartera_id": esc.cartera_id})),
        ("anotar_notas_cartera", "post", f"{esc.ruta}/notas", {"json": nota}, 201, (CarteraNota, {"cartera_id": esc.cartera_id})),
        ("actualizar_cliente", "patch", f"{esc.ruta}/clientes/{esc.cliente_id}", {"json": ubicacion}, 200, None),
    ]  # fmt: skip
    for permiso, metodo, url, kw, ok, tabla in casos:
        await _fijar_permisos(esc, set(TODOS) - {permiso})
        antes = await _contar(esc, tabla[0], **tabla[1]) if tabla else 0
        r = await getattr(app_client, metodo)(url, headers=esc.h(), **kw)
        assert r.status_code == 403, (permiso, r.text)
        assert r.json()["permiso"] == permiso
        if tabla:
            assert await _contar(esc, tabla[0], **tabla[1]) == antes, permiso

        await _fijar_permisos(esc, {permiso})
        r = await getattr(app_client, metodo)(url, headers=esc.h(), **kw)
        assert r.status_code == ok, (permiso, r.text)
        if tabla:
            assert await _contar(esc, tabla[0], **tabla[1]) == antes + 1, permiso


# ------------------------------------------------------------------ eliminar pago / abono
async def test_eliminar_pago_y_abono_exigen_su_permiso(app_client) -> None:
    from pack_cobranza.models import Abono, Pago

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    esc = await _escenario(app_client)
    async with tenant_scope(esc.tid):
        async with get_session() as session:
            from decimal import Decimal

            pago = Pago(tenant_id=esc.tid, cliente_id=esc.cliente_id, prestamo_id=esc.prestamo_id, valor=Decimal(1000), metodo_pago="efectivo", registrado_por="gestor")
            abono = Abono(tenant_id=esc.tid, prestamo_id=esc.prestamo_id, cliente_id=esc.cliente_id, valor=Decimal(1000), metodo_pago="efectivo", registrado_por="gestor")
            session.add_all([pago, abono])
            await session.flush()
            pago_id, abono_id = pago.id, abono.id
    for permiso, ruta, rid in (("eliminar_pago", "pagos", pago_id), ("eliminar_abono", "abonos", abono_id)):
        await _fijar_permisos(esc, set(TODOS) - {permiso})
        r = await app_client.request("DELETE", f"{esc.ruta}/{ruta}/{rid}", json={"password": PW}, headers=esc.h())
        assert r.status_code == 403, (permiso, r.text)
        assert r.json()["permiso"] == permiso
        await _fijar_permisos(esc, {permiso})
        r = await app_client.request("DELETE", f"{esc.ruta}/{ruta}/{rid}", json={"password": PW}, headers=esc.h())
        assert r.status_code == 200, (permiso, r.text)


async def test_liquidacion_exige_generar_reporte(app_client) -> None:
    esc = await _escenario(app_client)
    await _fijar_permisos(esc, set(TODOS) - {"generar_reporte"})
    r = await app_client.post(f"{esc.ruta}/liquidaciones", json={}, headers=esc.h())
    assert r.status_code == 403, r.text
    assert r.json()["permiso"] == "generar_reporte"


# ------------------------------------------------------------------ cola offline
async def test_sync_offline_aplica_la_matriz_por_evento(app_client) -> None:
    from pack_cobranza.models import Gasto, Pago

    esc = await _escenario(app_client)
    await _fijar_permisos(esc, {"registrar_gasto"})  # gasto sí, pago no
    pagos = await _contar(esc, Pago, prestamo_id=esc.prestamo_id)
    gastos = await _contar(esc, Gasto, cartera_id=esc.cartera_id)
    saldo = await _saldo(esc)
    ev_pago = uniq("ev-pago")
    ev_visita = uniq("ev-visita")
    ev_gasto = uniq("ev-gasto")
    eventos = {
        "eventos": [
            {"eventoIdCliente": ev_pago, "tipoEvento": "pago", "payload": _pago(esc, carteraId=str(esc.cartera_id))},
            {"eventoIdCliente": ev_visita, "tipoEvento": "visita", "payload": _pago(esc, carteraId=str(esc.cartera_id))},
            {"eventoIdCliente": ev_gasto, "tipoEvento": "gasto", "payload": {"carteraId": str(esc.cartera_id), "descripcion": "Peaje", "valor": 3000}},
        ]
    }  # fmt: skip

    r = await app_client.post(f"{BASE}/sync-offline/eventos", json=eventos, headers=esc.h())

    assert r.status_code == 200, r.text
    acks = {a["eventoIdCliente"]: a for a in r.json()}
    assert acks[ev_pago]["estado"] == "error" and "registrar_pago" in acks[ev_pago]["error"]
    assert acks[ev_visita]["estado"] == "error" and "registrar_pago" in acks[ev_visita]["error"]
    assert acks[ev_gasto]["estado"] == "sincronizado"
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == pagos
    assert await _contar(esc, Gasto, cartera_id=esc.cartera_id) == gastos + 1
    assert await _saldo(esc) == saldo

    # El evento rechazado por falta de permiso no queda como duplicado: con el permiso, el mismo
    # evento (mismo id) se aplica al reintentar.
    await _fijar_permisos(esc, {"registrar_pago"})
    r = await app_client.post(f"{BASE}/sync-offline/eventos", json={"eventos": [eventos["eventos"][0]]}, headers=esc.h())
    assert r.json()[0]["estado"] == "sincronizado", r.text
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == pagos + 1


async def test_sync_offline_con_permiso_aplica_el_pago(app_client) -> None:
    from pack_cobranza.models import Pago

    esc = await _escenario(app_client)
    await _fijar_permisos(esc, {"registrar_pago"})
    pagos = await _contar(esc, Pago, prestamo_id=esc.prestamo_id)
    ev = uniq("ev-pago-ok")
    r = await app_client.post(
        f"{BASE}/sync-offline/eventos",
        json={"eventos": [{"eventoIdCliente": ev, "tipoEvento": "pago", "payload": _pago(esc, carteraId=str(esc.cartera_id))}]},
        headers=esc.h(),
    )
    assert r.status_code == 200, r.text
    assert r.json()[0]["estado"] == "sincronizado", r.text
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == pagos + 1


# ------------------------------------------------------------------ propietario y gestor sin matriz
async def test_propietario_no_depende_de_la_matriz(app_client) -> None:
    from pack_cobranza.models import Pago

    esc = await _escenario(app_client)
    await _fijar_permisos(esc, set())  # matriz del gestor vacía: no afecta al propietario
    pagos = await _contar(esc, Pago, prestamo_id=esc.prestamo_id)

    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=_pago(esc), headers=esc.h(esc.token_owner))

    assert r.status_code == 201, r.text
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == pagos + 1


async def test_gestor_sin_filas_de_matriz_no_puede_escribir(app_client) -> None:
    """Denegado por defecto: sin ninguna fila en la matriz, no hay permiso para nada."""
    from pack_cobranza.models import GestorPermiso, Pago
    from sqlalchemy import delete

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    esc = await _escenario(app_client)
    async with tenant_scope(esc.tid):
        async with get_session() as session:
            await session.execute(delete(GestorPermiso).where(GestorPermiso.tenant_id == esc.tid))
    pagos = await _contar(esc, Pago, prestamo_id=esc.prestamo_id)

    r = await app_client.post(f"{esc.ruta}/visitas/pago", json=_pago(esc), headers=esc.h())

    assert r.status_code == 403, r.text
    assert await _contar(esc, Pago, prestamo_id=esc.prestamo_id) == pagos


# ------------------------------------------------------------------ multitenant
async def test_gestor_de_otro_negocio_no_registra_en_la_cartera_ajena(app_client) -> None:
    """Un gestor con TODOS los permisos en el negocio B no toca la cartera del negocio A."""
    from pack_cobranza.models import Pago

    a = await _escenario(app_client, "a")
    b = await _escenario(app_client, "b")
    await _fijar_permisos(b, set(TODOS))
    pagos_a = await _contar(a, Pago, prestamo_id=a.prestamo_id)

    r = await app_client.post(f"{a.ruta}/visitas/pago", json=_pago(a), headers=b.h())

    assert r.status_code == 404, r.text
    assert await _contar(a, Pago, prestamo_id=a.prestamo_id) == pagos_a


async def test_la_matriz_es_por_negocio_para_el_mismo_gestor(app_client) -> None:
    """Mismo admin gestor en A y B: el permiso de B no vale en A."""
    from pack_cobranza.models import Gestor, GestorPermiso
    from sqlalchemy import select

    from core.db.models import TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a = await _escenario(app_client, "a")
    b = await _escenario(app_client, "b")
    async with get_session() as session:
        from core.db.models import Admin

        admin_id = (await session.execute(select(Admin.id).where(Admin.email == a.gestor_email))).scalar_one()
    # El gestor de A también es gestor de B (con todos los permisos de B), con su cartera propia.
    async with tenant_scope(b.tid):
        async with get_session() as session:
            session.add(TenantUser(tenant_id=b.tid, admin_id=admin_id, role="colaborador"))
            g = Gestor(tenant_id=b.tid, admin_id=admin_id, nombre="Gestor compartido")
            session.add(g)
            await session.flush()
            session.add_all([GestorPermiso(tenant_id=b.tid, gestor_id=g.id, permiso=p, habilitado=True) for p in TODOS])
    await _fijar_permisos(a, set(TODOS) - {"registrar_pago"})

    token_a = await _login(app_client, a.gestor_email, a.slug)
    r = await app_client.post(f"{a.ruta}/visitas/pago", json=_pago(a), headers=a.h(token_a))

    assert r.status_code == 403, r.text
