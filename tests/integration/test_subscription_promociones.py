"""SPEC-43 · HU-SUB-33 · promociones de plan: canje, cupo, liberación y descuento del primer cobro, contra la BD real.

Lo que importa: el cobro sale NETO y el canje se reserva en la misma transacción; el pago del neto lo confirma;
TODO camino que anula un cobro devuelve el cupo; el último cupo lo gana uno solo aunque compitan varios; y un
código inválido responde siempre lo mismo.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_subscription_inscripcion import (
    DATOS,
    _cargos,
    _cliente,
    _inscribir,
    _negocio,
    _pago,
    _plan,
    _sub,
)

from core.shared.tenant import tenant_scope
from core.subscriptions import SubscriptionError

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CODIGO = "lanzamiento"


# ----------------------------------------------------------------------------- ayudas
async def _promo(tid: uuid.UUID, codigo: str | None = CODIGO, valor: str = "20", tipo: str = "porcentaje", **kw):
    from pack_subscriptions.promociones import DatosDePromocion, crear

    async with tenant_scope(tid):
        return await crear(
            DatosDePromocion(
                etiqueta=kw.pop("etiqueta", "Lanzamiento"),
                tipo_descuento=tipo,
                valor=Decimal(valor),
                codigo=codigo,
                **kw,
            ),
            actor="test",
        )


async def _promo_fila(tid: uuid.UUID, promo_id):
    from pack_subscriptions.models import Promocion

    from core.db.session import get_session

    async with tenant_scope(tid):
        async with get_session() as session:
            fila = (await session.execute(select(Promocion).where(Promocion.id == promo_id))).scalar_one()
            session.expunge_all()
            return fila


async def _canjes(tid: uuid.UUID, promo_id=None) -> list:
    from pack_subscriptions.models import Canje

    from core.db.session import get_session

    async with tenant_scope(tid):
        async with get_session() as session:
            consulta = select(Canje).order_by(Canje.created_at)
            if promo_id is not None:
                consulta = consulta.where(Canje.promocion_id == promo_id)
            filas = (await session.execute(consulta)).scalars().all()
            session.expunge_all()
            return list(filas)


async def _con_codigo(tid: uuid.UUID, cliente: uuid.UUID, plan: str = "BASIC", codigo: str = CODIGO, **kw):
    # Cada cliente con su teléfono: el teléfono es la huella cuando no hay documento (DATOS comparte uno).
    kw.setdefault("datos_alta", {**DATOS, "telefono": f"+57300{cliente.int % 10_000_000:07d}"})
    return await _inscribir(tid, cliente, plan, codigo_promocion=codigo, **kw)


async def _escenario(precio: str = "100000", **promo):
    """Negocio con un plan BASIC y una promoción del 20 %."""
    tid = await _negocio()
    plan = await _plan(tid, "BASIC", precio)
    p = await _promo(tid, **promo)
    return tid, plan, p


async def _error(coro) -> SubscriptionError:
    with pytest.raises(SubscriptionError) as e:
        await coro
    return e.value


# ============================================================================= canje al inscribir
async def test_inscribir_con_codigo_cobra_el_neto_y_reserva_el_canje() -> None:
    tid, _plan, promo = await _escenario()
    cliente = await _cliente(tid)

    vista = await _con_codigo(tid, cliente, monto_esperado=80000)

    assert vista["estado"] == "pendiente_pago"
    cobro = vista["cargo_inscripcion"]
    assert (cobro["monto"], cobro["monto_bruto"], cobro["descuento"]) == (80000.0, 100000.0, 20000.0)
    sub = await _sub(tid, uuid.UUID(vista["subscription_id"]))
    assert sub.valor_periodo == Decimal("100000.00"), "valor_periodo sigue siendo el precio de lista"
    [cargo] = await _cargos(tid, sub.id)
    assert (cargo.tipo, cargo.estado, cargo.monto, cargo.monto_bruto) == (
        "inscripcion", "pendiente", Decimal("80000.00"), Decimal("100000.00"),
    )
    assert cargo.descuento["promocion_id"] == str(promo.id) and cargo.descuento["descuento"] == "20000"
    assert cargo.periodo_n == 1 and cargo.consume_periodo is True
    [canje] = await _canjes(tid, promo.id)
    assert (canje.estado, canje.customer_id, canje.charge_id, canje.subscription_id) == (
        "reservado", cliente, cargo.id, sub.id,
    )
    assert cargo.canje_id == canje.id
    assert canje.promocion["etiqueta"] == "Lanzamiento" and canje.periodos_total == 1
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1


async def test_cotizar_con_codigo_muestra_el_descuento_y_no_deja_rastro() -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    tid, _, promo = await _escenario()
    async with tenant_scope(tid):
        sin = await SubscriptionCapabilityImpl().cotizar_inscripcion("BASIC")
        con = await SubscriptionCapabilityImpl().cotizar_inscripcion("BASIC", "  LANZAMIENTO ")
    assert (sin["descuento"], sin["total_primer_cargo"]) == (0.0, 100000.0)
    assert (con["precio_lista"], con["descuento"], con["total_primer_cargo"]) == (100000.0, 20000.0, 80000.0)
    assert con["precio_periodo_siguiente"] == 100000.0 and con["promocion"] == "Lanzamiento"
    assert await _canjes(tid) == [] and (await _promo_fila(tid, promo.id)).canjes_usados == 0


async def test_pagar_el_neto_confirma_el_canje_y_activa() -> None:
    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente)
    sub_id = uuid.UUID(vista["subscription_id"])
    [cargo] = await _cargos(tid, sub_id)

    await _pago(tid, cargo.id, 80000.0)

    assert (await _sub(tid, sub_id)).estado == "activa"
    [canje] = await _canjes(tid, promo.id)
    assert canje.estado == "agotado", "un descuento de un solo período queda terminal al pagarse (HU-SUB-35)"
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1
    [cargo] = await _cargos(tid, sub_id)
    assert cargo.estado == "pagado"


async def test_pagar_el_bruto_no_activa_ni_confirma() -> None:
    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente)
    sub_id = uuid.UUID(vista["subscription_id"])
    [cargo] = await _cargos(tid, sub_id)

    await _pago(tid, cargo.id, 100000.0)  # lo que costaba sin descuento: no es lo que se cobró

    assert (await _sub(tid, sub_id)).estado == "pendiente_pago"
    [cargo] = await _cargos(tid, sub_id)
    assert cargo.estado == "pendiente"
    assert [c.estado for c in await _canjes(tid, promo.id)] == ["reservado"]


async def test_el_pago_manual_tambien_confirma_el_canje() -> None:
    from pack_subscriptions.inscripcion import confirmar_pago

    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente, metodo_pago="transferencia")
    async with tenant_scope(tid):
        await confirmar_pago(uuid.UUID(vista["subscription_id"]), referencia="COMP-77", actor="admin:test")
    assert [c.estado for c in await _canjes(tid, promo.id)] == ["agotado"]


async def test_un_pago_que_llega_a_una_inscripcion_que_ya_no_espera_deja_el_canje_reservado() -> None:
    """El dinero llegó con el descuento pero el plan no se pudo aplicar: lo resuelve una persona. El canje NO se
    libera (el cobro con descuento sí se hizo) ni se confirma; queda a la vista del gauge de reservados antiguos."""
    from core.db.session import get_session

    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente)
    sub_id = uuid.UUID(vista["subscription_id"])
    [cargo] = await _cargos(tid, sub_id)
    async with tenant_scope(tid):
        async with get_session() as session:  # un estado que no debería darse con cobro pendiente: hay que blindarlo igual
            await session.execute(text("update pack_subscriptions_subscriptions set estado='pausada' where id=:i"), {"i": sub_id})

    await _pago(tid, cargo.id, 80000.0)

    [cargo] = await _cargos(tid, sub_id)
    assert cargo.estado == "pagado"
    assert (await _sub(tid, sub_id)).estado == "pausada"
    assert [c.estado for c in await _canjes(tid, promo.id)] == ["reservado"]
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1


# ============================================================================= liberar en TODOS los sitios que anulan
async def _inscripcion_pendiente(tid: uuid.UUID):
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente)
    sub_id = uuid.UUID(vista["subscription_id"])
    return cliente, sub_id, (await _cargos(tid, sub_id))[0]


async def _free_con_cambio_pendiente(tid: uuid.UUID, *, trial_dias: int = 14):
    """Cliente en un plan Free que pidió pasar a BASIC con el código: cobro `cambio_plan` neto y canje reservado."""
    await _plan(tid, "FREE", "0", trial_dias=trial_dias)
    cliente = await _cliente(tid)
    free = await _inscribir(tid, cliente, "FREE")
    vista = await _con_codigo(tid, cliente)
    assert vista["inscripcion"] == "cambio_de_plan"
    sub_id = uuid.UUID(free["subscription_id"])
    cargo = next(c for c in await _cargos(tid, sub_id) if c.tipo == "cambio_plan")
    return cliente, sub_id, cargo


async def _expirar(tid, sub_id, _cliente_id):
    from pack_subscriptions.inscripcion import expirar_una

    async with tenant_scope(tid):
        assert await expirar_una(sub_id, limite=datetime.now(UTC) + timedelta(minutes=1))


async def _cancelar(tid, sub_id, _cliente_id):
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    async with tenant_scope(tid):
        await SubscriptionCapabilityImpl().cancel(sub_id)


async def _reemplazar(tid, _sub_id, cliente_id):
    await _plan(tid, "OTRO", "150000")
    await _inscribir(tid, cliente_id, "OTRO")


async def _cambio_reemplazado(tid, _sub_id, cliente_id):
    await _plan(tid, "OTRO", "150000")
    await _inscribir(tid, cliente_id, "OTRO")  # la Free pide otro plan: el cambio anterior se anula


async def _ciclo_de_cobro(tid, sub_id, _cliente_id):
    """El cron de cargos llega al fin del período de la Free: el cambio pendiente ya no aplica y se anula."""
    from pack_subscriptions.jobs import generar_cargo_de
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session

    async with tenant_scope(tid):
        async with get_session() as session:
            sub = (await session.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
            assert sub.proximo_cargo is not None
            await generar_cargo_de(session, tid, sub, sub.proximo_cargo + timedelta(days=1), cap=None, config=None)


@pytest.mark.parametrize(
    ("preparar", "provocar", "esperado_sub"),
    [
        pytest.param(_inscripcion_pendiente, _expirar, "cancelada", id="expiracion"),
        pytest.param(_inscripcion_pendiente, _cancelar, "cancelada", id="cancelar_la_inscripcion"),
        pytest.param(_inscripcion_pendiente, _reemplazar, "cancelada", id="reemplazo_por_otro_plan"),
        pytest.param(_free_con_cambio_pendiente, _cambio_reemplazado, None, id="cambio_de_plan_reemplazado"),
        pytest.param(_free_con_cambio_pendiente, _cancelar, "cancelada", id="cancelar_la_free_con_cambio_pendiente"),
        pytest.param(
            lambda tid: _free_con_cambio_pendiente(tid, trial_dias=0), _ciclo_de_cobro, None, id="ciclo_de_cobro",
        ),
    ],
)
async def test_todo_sitio_que_anula_un_cobro_libera_el_canje(preparar, provocar, esperado_sub) -> None:
    tid, _, promo = await _escenario()
    cliente, sub_id, cargo = await preparar(tid)
    assert [c.estado for c in await _canjes(tid, promo.id)] == ["reservado"]
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1

    await provocar(tid, sub_id, cliente)

    cargos = {c.id: c for c in await _cargos(tid, sub_id)}
    assert cargos[cargo.id].estado == "anulado"
    assert [c.estado for c in await _canjes(tid, promo.id)] == ["liberado"]
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 0
    if esperado_sub:
        assert (await _sub(tid, sub_id)).estado == esperado_sub


async def test_liberar_dos_veces_no_descuenta_dos_veces() -> None:
    """El cron y el webhook pueden anular a la vez: la liberación es condicional al estado `reservado`."""
    from pack_subscriptions.inscripcion import anular_cargos_pendientes
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session

    tid, _, promo = await _escenario()
    _, sub_id, _ = await _inscripcion_pendiente(tid)

    async def anular() -> None:
        async with tenant_scope(tid):
            async with get_session() as session:
                sub = (await session.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
                await anular_cargos_pendientes(session, sub)

    await asyncio.gather(anular(), anular())
    await anular()

    assert (await _promo_fila(tid, promo.id)).canjes_usados == 0
    assert [c.estado for c in await _canjes(tid, promo.id)] == ["liberado"]


# ============================================================================= un canje por cliente y por persona
async def test_un_canje_por_cliente_y_cancelar_no_lo_recupera() -> None:
    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente)
    sub_id = uuid.UUID(vista["subscription_id"])
    [cargo] = await _cargos(tid, sub_id)
    await _pago(tid, cargo.id, 80000.0)
    await _cancelar(tid, sub_id, cliente)

    error = await _error(_con_codigo(tid, cliente))

    assert error.code == "promo_ya_usada"
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1


async def test_un_canje_liberado_permite_volver_a_canjear() -> None:
    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente)
    await _cancelar(tid, uuid.UUID(vista["subscription_id"]), cliente)

    otra = await _con_codigo(tid, cliente)

    assert otra["cargo_inscripcion"]["monto"] == 80000.0
    assert sorted(c.estado for c in await _canjes(tid, promo.id)) == ["liberado", "reservado"]
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1


async def test_la_misma_persona_con_otro_cliente_no_canjea_otra_vez() -> None:
    """Mismo documento en otro cliente (otro número de teléfono) = misma huella = `promo_ya_usada`."""
    tid, _, promo = await _escenario()
    datos = {**DATOS, "documento": "CC 1.234.567"}
    primero, segundo, tercero = await _cliente(tid), await _cliente(tid), await _cliente(tid)
    await _con_codigo(tid, primero, datos_alta=datos)

    error = await _error(_con_codigo(tid, segundo, datos_alta={**datos, "telefono": "+573007776655"}))
    assert error.code == "promo_ya_usada"

    ok = await _con_codigo(tid, tercero, datos_alta={**DATOS, "documento": "CC 7.654.321"})
    assert ok["cargo_inscripcion"]["monto"] == 80000.0
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 2


async def test_la_huella_no_guarda_el_documento_en_claro() -> None:
    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)
    await _con_codigo(tid, cliente, datos_alta={**DATOS, "documento": "CC 1.234.567"})
    [canje] = await _canjes(tid, promo.id)
    assert canje.huella and canje.huella.startswith("doc:") and "1234567" not in canje.huella


# ============================================================================= último cupo
async def test_el_ultimo_cupo_lo_gana_uno_solo_aunque_compitan_varios() -> None:
    tid, _, promo = await _escenario(max_canjes=3)
    clientes = [await _cliente(tid, f"C{i}") for i in range(8)]

    resultados = await asyncio.gather(*[_con_codigo(tid, c) for c in clientes], return_exceptions=True)

    ganadores = [r for r in resultados if isinstance(r, dict)]
    perdedores = [r for r in resultados if not isinstance(r, dict)]
    assert all(isinstance(e, SubscriptionError) and e.code in ("promo_agotada", "codigo_no_valido") for e in perdedores), perdedores
    assert len(ganadores) == 3
    fila = await _promo_fila(tid, promo.id)
    assert fila.canjes_usados == 3, "canjes_usados nunca excede max_canjes"
    assert len([c for c in await _canjes(tid, promo.id) if c.estado != "liberado"]) == 3
    # Los que perdieron no dejaron una suscripción a medias.
    huerfanas = 0
    for cliente, resultado in zip(clientes, resultados, strict=True):
        if not isinstance(resultado, dict):
            from pack_subscriptions.models import Subscription

            from core.db.session import get_session

            async with tenant_scope(tid):
                async with get_session() as session:
                    huerfanas += len(
                        (await session.execute(select(Subscription.id).where(Subscription.customer_id == cliente))).all()
                    )
    assert huerfanas == 0


async def test_reservar_sin_cupo_responde_promo_agotada() -> None:
    """Dos evaluaron con un cupo y el otro se gastó antes de reservar: el UPDATE atómico lo detecta."""
    from pack_subscriptions import promociones
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.db.session import get_session

    tid, plan, promo = await _escenario(max_canjes=1)
    primero, segundo = await _cliente(tid), await _cliente(tid)
    ahora = datetime.now(UTC)
    async with tenant_scope(tid):
        async with get_session() as session:
            evaluacion = await promociones.evaluar(
                session, codigo=CODIGO, plan_id=plan.id, precio=Decimal("100000"), moneda="COP",
                customer_id=segundo, huella_=None, ahora=ahora, contar_intento=False,
            )
    await _con_codigo(tid, primero)  # se lleva el único cupo después de la evaluación del segundo

    with pytest.raises(SubscriptionError) as e:
        async with tenant_scope(tid):
            async with get_session() as session:
                sub = Subscription(
                    tenant_id=tid, customer_id=segundo, plan_id=plan.id, concepto="X", valor_periodo=Decimal("100000"),
                    estado="pendiente_pago",
                )
                session.add(sub)
                await session.flush()
                cargo = SubscriptionCharge(
                    tenant_id=tid, subscription_id=sub.id, periodo="inscripcion", tipo="inscripcion", monto=Decimal("100000")
                )
                promociones.aplicar_al_cargo(cargo, evaluacion)
                session.add(cargo)
                await session.flush()
                await promociones.reservar(session, evaluacion, sub=sub, cargo=cargo, customer_id=segundo, huella_=None)
    assert e.value.code == "promo_agotada"
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1


# ============================================================================= código no válido, Free y neto 0
async def test_un_codigo_no_valido_responde_lo_mismo_se_sepa_o_no_por_que() -> None:
    tid = await _negocio()
    await _plan(tid, "BASIC", "100000")
    await _plan(tid, "PRO", "200000")
    basic = await _plan_id(tid, "BASIC")
    ahora = datetime.now(UTC)
    await _promo(tid, "vencida", vigente_desde=ahora - timedelta(days=10), vigente_hasta=ahora - timedelta(days=1))
    await _promo(tid, "futura", vigente_desde=ahora + timedelta(days=1))
    await _promo(tid, "apagada", activa=False)
    await _promo(tid, "agotada", max_canjes=1)
    await _promo(tid, "soloenpro", plan_id=await _plan_id(tid, "PRO"))
    otro = await _cliente(tid)
    await _con_codigo(tid, otro, codigo="agotada")  # se lleva el único cupo
    assert basic

    mensajes = set()
    for codigo in ("inexistente", "vencida", "futura", "apagada", "agotada", "soloenpro", "con espacio", "x"):
        error = await _error(_con_codigo(tid, await _cliente(tid), codigo=codigo))
        assert error.code == "codigo_no_valido", codigo
        mensajes.add(str(error))
    assert len(mensajes) == 1, "el mensaje no distingue por qué"
    # Ninguno de los rechazos dejó una suscripción ni consumió cupo.
    assert len(await _canjes(tid)) == 1


async def _plan_id(tid: uuid.UUID, codigo: str):
    from pack_subscriptions import planes

    async with tenant_scope(tid):
        return (await planes.por_codigo(codigo)).id


async def test_el_plan_free_no_recibe_promociones() -> None:
    tid = await _negocio()
    await _plan(tid, "FREE", "0", trial_dias=14)
    await _promo(tid)
    error = await _error(_con_codigo(tid, await _cliente(tid), plan="FREE"))
    assert error.code == "promo_no_aplica_a_free"


async def test_un_descuento_que_deja_el_neto_en_cero_se_rechaza() -> None:
    tid, _, promo = await _escenario(precio="50000", codigo="todo", valor="50000", tipo="monto")
    cliente = await _cliente(tid)
    error = await _error(_con_codigo(tid, cliente, codigo="todo"))
    assert error.code == "descuento_excede_precio"
    assert await _canjes(tid) == [] and (await _promo_fila(tid, promo.id)).canjes_usados == 0


async def test_monto_esperado_se_compara_con_el_neto() -> None:
    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)
    error = await _error(_con_codigo(tid, cliente, monto_esperado=100000))  # el precio de lista, no el neto
    assert error.code == "monto_distinto"
    assert await _canjes(tid) == [] and (await _promo_fila(tid, promo.id)).canjes_usados == 0
    assert (await _con_codigo(tid, cliente, monto_esperado=80000))["estado"] == "pendiente_pago"


async def test_el_umbral_de_monto_alto_se_evalua_sobre_el_precio_de_lista() -> None:
    from core.plugin_manager import plugin_manager

    tid, _, _ = await _escenario()
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_subscriptions", {"monto_alto_inscripcion": 90000})
    # El neto (80.000) queda bajo el umbral, pero el precio de lista (100.000) lo supera.
    error = await _error(_con_codigo(tid, await _cliente(tid), origen="agente", terminos={}))
    assert error.code == "monto_alto" and error.escalate


# ============================================================================= Free → pago con promoción
async def test_free_a_pago_con_codigo_lleva_el_descuento_y_cuenta_como_el_periodo_1() -> None:
    tid, plan, promo = await _escenario()
    cliente = await _cliente(tid)
    await _plan(tid, "FREE", "0", trial_dias=14)
    free = await _inscribir(tid, cliente, "FREE")
    sub_id = uuid.UUID(free["subscription_id"])

    vista = await _con_codigo(tid, cliente, monto_esperado=80000)

    assert vista["inscripcion"] == "cambio_de_plan" and vista["cargo_cambio"]["monto"] == 80000.0
    cargo = next(c for c in await _cargos(tid, sub_id) if c.tipo == "cambio_plan")
    assert (cargo.monto, cargo.monto_bruto, cargo.periodo_n, cargo.consume_periodo) == (
        Decimal("80000.00"), Decimal("100000.00"), 1, True,
    )
    assert cargo.cambio_plan["desde_cero"] is True
    [canje] = await _canjes(tid, promo.id)
    assert (canje.estado, canje.charge_id, canje.subscription_id) == ("reservado", cargo.id, sub_id)

    # Repetir lo mismo reusa el cobro y no reserva otro canje.
    otra = await _con_codigo(tid, cliente, monto_esperado=80000)
    assert otra["cargo_cambio"]["monto"] == 80000.0
    assert len([c for c in await _cargos(tid, sub_id) if c.tipo == "cambio_plan" and c.estado == "pendiente"]) == 1
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1

    await _pago(tid, cargo.id, 80000.0)
    sub = await _sub(tid, sub_id)
    assert sub.estado == "activa" and sub.plan_id == plan.id and sub.valor_periodo == Decimal("100000.00")
    assert [c.estado for c in await _canjes(tid, promo.id)] == ["agotado"]


async def test_change_plan_con_codigo_aplica_desde_cero_y_un_downgrade_lo_rechaza() -> None:
    from pack_subscriptions import planes
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    tid, plan, promo = await _escenario()
    bajo = await _plan(tid, "BAJO", "50000")
    await _plan(tid, "FREE", "0", trial_dias=14)
    cliente = await _cliente(tid)
    free = await _inscribir(tid, cliente, "FREE")
    sub_id = uuid.UUID(free["subscription_id"])
    cap = SubscriptionCapabilityImpl()

    async with tenant_scope(tid):
        vista = await cap.change_plan(
            sub_id, variant_id=str(plan.id), plan=planes.snapshot(plan), monto_esperado=80000, codigo_promocion=CODIGO
        )
    assert vista["cambio"] == "pendiente_pago" and vista["cargo_cambio"]["monto"] == 80000.0
    assert vista["cargo_cambio"]["monto_bruto"] == 100000.0
    assert [c.estado for c in await _canjes(tid, promo.id)] == ["reservado"]

    # Pagado el cambio, la suscripción ya es de pago: bajar de plan con un código se rechaza, no se ignora.
    cargo = next(c for c in await _cargos(tid, sub_id) if c.tipo == "cambio_plan")
    await _pago(tid, cargo.id, 80000.0)
    otra_promo = await _promo(tid, "otra", valor="10")
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as e:
            await cap.change_plan(sub_id, variant_id=str(bajo.id), plan=planes.snapshot(bajo), codigo_promocion="otra")
    assert e.value.code == "promo_no_aplica_a_downgrade"
    assert (await _promo_fila(tid, otra_promo.id)).canjes_usados == 0


# ============================================================================= replay
async def test_repetir_con_el_mismo_codigo_devuelve_el_mismo_cobro_y_otro_codigo_se_rechaza() -> None:
    tid, _, promo = await _escenario()
    await _promo(tid, "otro10", valor="10")
    cliente = await _cliente(tid)
    primera = await _con_codigo(tid, cliente)

    segunda = await _con_codigo(tid, cliente, monto_esperado=80000)
    assert segunda["inscripcion"] == "repetida" and segunda["subscription_id"] == primera["subscription_id"]
    assert segunda["cargo_inscripcion"]["monto"] == 80000.0 and segunda["cargo_inscripcion"]["descuento"] == 20000.0

    for codigo in ("otro10", "no-existe"):
        error = await _error(_con_codigo(tid, cliente, codigo=codigo))
        assert error.code == "promo_distinta", "un código inexistente y uno de otra promoción responden igual"
    sin_codigo = await _inscribir(tid, cliente, "BASIC")
    assert sin_codigo["inscripcion"] == "repetida" and sin_codigo["cargo_inscripcion"]["monto"] == 80000.0
    error = await _error(_con_codigo(tid, cliente, monto_esperado=100000))
    assert error.code == "monto_distinto"
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1 and len(await _canjes(tid, promo.id)) == 1


async def test_la_misma_inscripcion_con_codigo_a_la_vez_reserva_un_solo_canje() -> None:
    tid, _, promo = await _escenario()
    cliente = await _cliente(tid)

    vistas = await asyncio.gather(*[_con_codigo(tid, cliente) for _ in range(6)])

    assert len({v["subscription_id"] for v in vistas}) == 1
    assert len(await _canjes(tid, promo.id)) == 1
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 1


# ============================================================================= abuso y observabilidad
async def test_los_intentos_con_codigos_tienen_tope_por_cliente() -> None:
    tid, _, _ = await _escenario()
    cliente = await _cliente(tid)
    for i in range(10):
        assert (await _error(_con_codigo(tid, cliente, codigo=f"nope-{i}"))).code == "codigo_no_valido"

    error = await _error(_con_codigo(tid, cliente, codigo=CODIGO))  # ni el código bueno pasa: no se evalúa

    assert error.code == "demasiados_intentos"
    # Otro cliente del mismo negocio no se ve afectado.
    assert (await _con_codigo(tid, await _cliente(tid)))["estado"] == "pendiente_pago"


async def test_el_gauge_cuenta_los_canjes_reservados_pasado_el_plazo() -> None:
    from pack_subscriptions import promociones

    from core.db.session import get_session

    tid, _, promo = await _escenario()
    await _con_codigo(tid, await _cliente(tid))
    await _con_codigo(tid, await _cliente(tid))
    async with tenant_scope(tid):
        async with get_session() as session:
            assert await promociones.contar_reservados_antiguos(session, dias=7) == 0
            await session.execute(
                text("update pack_subscriptions_canjes set created_at = now() - interval '9 days' where id = (select id from pack_subscriptions_canjes limit 1)")
            )
    async with tenant_scope(tid):
        async with get_session() as session:
            assert await promociones.contar_reservados_antiguos(session, dias=7) == 1
    assert (await _promo_fila(tid, promo.id)).canjes_usados == 2


# ============================================================================= aislamiento y esquema
async def test_un_negocio_no_ve_ni_canjea_las_promociones_de_otro() -> None:
    from pack_subscriptions import promociones

    from core.db.session import get_session

    a, plan_a, promo_a = await _escenario()
    b = await _negocio()
    await _plan(b, "BASIC", "100000")
    await _con_codigo(a, await _cliente(a))

    error = await _error(_con_codigo(b, await _cliente(b)))  # el código de A en el negocio B
    assert error.code == "codigo_no_valido"
    async with tenant_scope(b):
        assert await promociones.listar() == [] and await promociones.obtener(promo_a.id) is None
        async with get_session() as session:
            for tabla in ("pack_subscriptions_promociones", "pack_subscriptions_canjes"):
                assert (await session.execute(text(f"select count(*) from {tabla}"))).scalar_one() == 0
        # Ni se puede colgar una promoción de B de un plan de A (FK compuesta con el negocio).
        with pytest.raises(IntegrityError):
            async with get_session() as session:
                await session.execute(
                    text(
                        "insert into pack_subscriptions_promociones (id, tenant_id, etiqueta, tipo_descuento, valor, plan_id) "
                        "values (gen_random_uuid(), :t, 'x', 'porcentaje', 10, :p)"
                    ),
                    {"t": b, "p": plan_a.id},
                )
    async with tenant_scope(a):
        assert len(await promociones.listar()) == 1


@pytest.mark.parametrize(
    "valores",
    [
        "'porcentaje', 100, 1, null",  # 100 % no es un cobro
        "'porcentaje', 0, 1, null",
        "'monto', 5000, 2, null",  # un monto fijo solo con 1 período
        "'porcentaje', 10, 13, null",
        "'porcentaje', 10, 1, 0",  # tope de canjes no positivo
        "'regalo', 10, 1, null",
    ],
)
async def test_la_base_impide_promociones_invalidas(valores) -> None:
    from core.db.session import get_session

    tid = await _negocio()
    with pytest.raises(IntegrityError):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text(
                        "insert into pack_subscriptions_promociones "
                        "(id, tenant_id, etiqueta, tipo_descuento, valor, periodos, max_canjes) "
                        f"values (gen_random_uuid(), :t, 'x', {valores})"
                    ),
                    {"t": tid},
                )


async def test_la_base_impide_un_neto_mayor_que_el_bruto_y_un_descuento_sin_bruto() -> None:
    from core.db.session import get_session

    tid, _, _ = await _escenario()
    vista = await _con_codigo(tid, await _cliente(tid))
    [cargo] = await _cargos(tid, uuid.UUID(vista["subscription_id"]))
    # El cobro ya trae descuento (monto 80.000, bruto 100.000): estas ediciones rompen el par o el neto.
    for cambio in ("monto_bruto = 50000", "monto = 120000", "monto = 0", "monto_bruto = null", "descuento = null"):
        with pytest.raises(IntegrityError):
            async with tenant_scope(tid):
                async with get_session() as session:
                    await session.execute(
                        text(f"update pack_subscriptions_charges set {cambio} where id = :i"), {"i": cargo.id}
                    )
