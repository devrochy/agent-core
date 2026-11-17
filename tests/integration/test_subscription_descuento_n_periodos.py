"""SPEC-43 · HU-SUB-35 · descuento de N períodos: consumo en cada cargo, cambio de plan y reemplazo, contra la BD real.

Lo que importa: cada período con descuento se consume UNA vez (aunque corran dos cobros a la vez o falle una
corrida), el canje recorre `reservado → confirmado → agotado|terminado` por cada camino, cualquier cambio de plan
termina el descuento vigente, y `restantes` siempre cuadra con los cargos que consumieron período.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_subscription_inscripcion import (
    _cargos,
    _cliente,
    _inscribir,
    _negocio,
    _pago,
    _plan,
    _sub,
)
from test_subscription_promociones import CODIGO, _canjes, _con_codigo, _promo

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


# ----------------------------------------------------------------------------- ayudas
async def _activa_con_descuento(periodos: int = 3, valor: str = "50", precio: str = "100000", **promo):
    """Negocio con BASIC y una promoción de `periodos` al `valor` %; un cliente la canjea y paga la inscripción
    (período 1). Devuelve `(tid, plan, promocion, cliente, sub_id)`."""
    tid = await _negocio()
    plan = await _plan(tid, "BASIC", precio)
    promocion = await _promo(tid, valor=valor, periodos=periodos, **promo)
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente)
    sub_id = uuid.UUID(vista["subscription_id"])
    [cargo] = await _cargos(tid, sub_id)
    await _pago(tid, cargo.id, float(cargo.monto))
    return tid, plan, promocion, cliente, sub_id


async def _correr_cron(tid: uuid.UUID, sub_id: uuid.UUID) -> None:
    """Una corrida del cron de cobros para este negocio, en la fecha del próximo cargo de la suscripción."""
    from pack_subscriptions.jobs import _generar_cargos_del_negocio

    sub = await _sub(tid, sub_id)
    await _generar_cargos_del_negocio(tid, sub.proximo_cargo)


async def _periodos(tid: uuid.UUID, sub_id: uuid.UUID) -> list:
    return [c for c in await _cargos(tid, sub_id) if c.tipo == "periodo"]


async def _estados_canje(tid: uuid.UUID, promo_id) -> list[str]:
    return [c.estado for c in await _canjes(tid, promo_id)]


async def _cap():
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    return SubscriptionCapabilityImpl()


# ============================================================================= consumo en cada cargo
async def test_tres_meses_al_50_por_ciento_salen_a_mitad_de_precio_y_el_cuarto_a_lista() -> None:
    tid, _, promo, _, sub_id = await _activa_con_descuento(periodos=3)
    sub = await _sub(tid, sub_id)
    assert sub.descuento_periodos_restantes == 2 and sub.promocion_id == promo.id
    assert await _estados_canje(tid, promo.id) == ["confirmado"]

    await _correr_cron(tid, sub_id)
    [c2] = await _periodos(tid, sub_id)
    assert (c2.monto, c2.monto_bruto, c2.periodo_n, c2.consume_periodo) == (Decimal("50000"), Decimal("100000"), 2, True)
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 1

    await _correr_cron(tid, sub_id)
    c3 = (await _periodos(tid, sub_id))[-1]
    assert (c3.monto, c3.periodo_n) == (Decimal("50000"), 3)
    sub = await _sub(tid, sub_id)
    assert (sub.promocion_id, sub.descuento, sub.descuento_periodos_restantes) == (None, None, None)
    assert await _estados_canje(tid, promo.id) == ["agotado"]

    await _correr_cron(tid, sub_id)
    c4 = (await _periodos(tid, sub_id))[-1]
    assert (c4.monto, c4.monto_bruto, c4.descuento, c4.consume_periodo) == (Decimal("100000"), None, None, False)
    assert len(await _periodos(tid, sub_id)) == 3


async def test_una_promocion_de_un_periodo_queda_agotada_al_pagar_y_no_toca_los_cargos_siguientes() -> None:
    tid, _, promo, _, sub_id = await _activa_con_descuento(periodos=1)
    assert await _estados_canje(tid, promo.id) == ["agotado"]
    assert (await _sub(tid, sub_id)).promocion_id is None
    await _correr_cron(tid, sub_id)
    [c2] = await _periodos(tid, sub_id)
    assert c2.monto == Decimal("100000") and c2.monto_bruto is None


async def test_dos_corridas_a_la_vez_dejan_un_solo_cargo_y_un_solo_consumo() -> None:
    from pack_subscriptions.jobs import _generar_cargos_del_negocio

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    ahora = (await _sub(tid, sub_id)).proximo_cargo

    await asyncio.gather(*[_generar_cargos_del_negocio(tid, ahora) for _ in range(4)])

    assert len(await _periodos(tid, sub_id)) == 1
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 1


async def test_crear_el_cargo_del_mismo_periodo_a_la_vez_consume_un_solo_periodo() -> None:
    """El creador único: las suscripciones se bloquean en serie y el segundo ve el cargo ya creado."""
    from pack_subscriptions.cargos import crear_cargo_de_periodo
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    inicio = (await _sub(tid, sub_id)).proximo_cargo

    async def crear() -> bool:
        async with tenant_scope(tid), get_session() as session:
            sub = (
                await session.execute(select(Subscription).where(Subscription.id == sub_id).with_for_update())
            ).scalar_one()
            _, creado = await crear_cargo_de_periodo(session, sub, inicio)
            return creado

    creados = await asyncio.gather(*[crear() for _ in range(5)])

    assert sorted(creados).count(True) == 1
    assert len(await _periodos(tid, sub_id)) == 1
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 1


async def test_el_unico_de_periodo_impide_un_segundo_cargo_aunque_nadie_bloquee() -> None:
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    await _correr_cron(tid, sub_id)
    [existente] = await _periodos(tid, sub_id)

    with pytest.raises(IntegrityError, match="uq_pack_subscriptions_charges_periodo"):
        async with tenant_scope(tid), get_session() as session:
            session.add(
                SubscriptionCharge(
                    tenant_id=tid, subscription_id=sub_id, periodo=existente.periodo, monto=Decimal("100000"),
                    estado="pendiente",
                )
            )
            await session.flush()


async def test_un_fallo_a_mitad_de_la_generacion_no_pierde_ni_duplica_un_periodo(monkeypatch) -> None:
    from pack_subscriptions import jobs

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    ahora = (await _sub(tid, sub_id)).proximo_cargo
    original = jobs.paga_por_otro_canal

    async def falla(*args, **kwargs):
        raise RuntimeError("caída a mitad de la generación")

    monkeypatch.setattr(jobs, "paga_por_otro_canal", falla)
    with pytest.raises(RuntimeError):
        await jobs._generar_cargos_del_negocio(tid, ahora)
    # Todo se deshizo junto: ni cargo, ni período consumido, ni calendario avanzado.
    assert await _periodos(tid, sub_id) == []
    sub = await _sub(tid, sub_id)
    assert sub.descuento_periodos_restantes == 2 and sub.proximo_cargo == ahora

    monkeypatch.setattr(jobs, "paga_por_otro_canal", original)
    await jobs._generar_cargos_del_negocio(tid, ahora)
    assert len(await _periodos(tid, sub_id)) == 1
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 1


async def test_un_negocio_que_falla_no_frena_a_los_demas(monkeypatch) -> None:
    from pack_subscriptions import jobs

    malo, _, _, _, sub_malo = await _activa_con_descuento(periodos=3)
    bueno, _, _, _, sub_bueno = await _activa_con_descuento(periodos=3)
    ahora = max((await _sub(malo, sub_malo)).proximo_cargo, (await _sub(bueno, sub_bueno)).proximo_cargo)
    original = jobs._generar_cargos_del_negocio

    async def selectivo(tenant_id, now):
        if tenant_id == malo:
            raise RuntimeError("este negocio falla")
        return await original(tenant_id, now)

    monkeypatch.setattr(jobs, "_generar_cargos_del_negocio", selectivo)
    await jobs._generar_cargos(ahora)  # no lanza

    assert len(await _periodos(bueno, sub_bueno)) == 1
    assert await _periodos(malo, sub_malo) == []


async def test_un_pago_tardio_cobra_el_neto_original() -> None:
    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    await _correr_cron(tid, sub_id)
    [c2] = await _periodos(tid, sub_id)

    await _pago(tid, c2.id, 50000.0)  # el neto, aunque llegue tarde

    c2 = (await _periodos(tid, sub_id))[0]
    assert c2.estado == "pagado" and c2.monto == Decimal("50000")
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 1, "pagar no consume otra vez"


async def test_pausa_y_reactivacion_no_consumen_periodos() -> None:
    from pack_subscriptions.jobs import _generar_cargos_del_negocio

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    vencimiento = (await _sub(tid, sub_id)).proximo_cargo
    cap = await _cap()
    async with tenant_scope(tid):
        await cap.pause(sub_id, reason="viaje")
    await _generar_cargos_del_negocio(tid, vencimiento)
    await _generar_cargos_del_negocio(tid, vencimiento)
    assert await _periodos(tid, sub_id) == []
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 2

    async with tenant_scope(tid):
        await cap.resume(sub_id)
    await _correr_cron(tid, sub_id)
    [c2] = await _periodos(tid, sub_id)
    assert c2.periodo_n == 2 and (await _sub(tid, sub_id)).descuento_periodos_restantes == 1


async def test_el_cobro_de_inscripcion_pagado_no_se_vuelve_a_cobrar_con_el_link_del_periodo() -> None:
    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    cap = await _cap()
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as e:
            await cap.charge_link(sub_id)
    assert e.value.code == "ya_pagado"
    assert await _periodos(tid, sub_id) == []


async def test_un_pago_externo_se_crea_a_precio_de_lista_y_no_consume() -> None:
    from datetime import timedelta

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    sub = await _sub(tid, sub_id)
    cap = await _cap()
    async with tenant_scope(tid):
        r = await cap.registrar_pago_externo(
            sub_id, inicio=sub.proximo_cargo, fin=sub.proximo_cargo + timedelta(days=30),
            referencia="TRF-1", canal="transferencia",
        )
    assert r["estado"] == "registrado"
    [cargo] = await _periodos(tid, sub_id)
    assert (cargo.monto, cargo.monto_bruto, cargo.consume_periodo) == (Decimal("100000"), None, False)
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 2


# ============================================================================= cancelar y cambiar de plan
async def test_cancelar_termina_el_descuento_y_no_se_puede_reutilizar_con_otra_inscripcion() -> None:
    tid, _, promo, cliente, sub_id = await _activa_con_descuento(periodos=3)
    cap = await _cap()
    async with tenant_scope(tid):
        await cap.cancel(sub_id, reason="ya no")

    sub = await _sub(tid, sub_id)
    assert (sub.promocion_id, sub.descuento, sub.descuento_periodos_restantes) == (None, None, None)
    assert await _estados_canje(tid, promo.id) == ["terminado"]
    with pytest.raises(SubscriptionError) as e:
        await _con_codigo(tid, cliente)
    assert e.value.code == "promo_ya_usada"


async def _dos_planes(tid: uuid.UUID):
    return await _plan(tid, "PRO", "300000")


async def test_cualquier_cambio_de_plan_termina_el_descuento_vigente() -> None:
    from pack_subscriptions import planes

    tid, _, promo, _, sub_id = await _activa_con_descuento(periodos=3)
    pro = await _dos_planes(tid)
    cap = await _cap()
    async with tenant_scope(tid):
        cotiza = await cap.cotizar_cambio(sub_id, plan_id=str(pro.id), plan=planes.snapshot(pro))
        assert cotiza["reemplaza_descuento"]["periodos_restantes"] == 2
        vista = await cap.change_plan(
            sub_id, plan_id=str(pro.id), plan=planes.snapshot(pro), monto_esperado=cotiza["monto_a_pagar"]
        )
    # Mientras no se paga, conserva el descuento anterior.
    assert vista["cambio"] == "pendiente_pago"
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 2
    cargo = next(c for c in await _cargos(tid, sub_id) if c.tipo == "cambio_plan")

    await _pago(tid, cargo.id, float(cargo.monto))

    sub = await _sub(tid, sub_id)
    assert sub.plan_id == pro.id and sub.promocion_id is None and sub.descuento_periodos_restantes is None
    assert await _estados_canje(tid, promo.id) == ["terminado"]


async def test_un_downgrade_programado_termina_el_descuento_al_aplicarse() -> None:
    from pack_subscriptions import planes

    tid, _, promo, _, sub_id = await _activa_con_descuento(periodos=3)
    bajo = await _plan(tid, "BAJO", "50000")
    cap = await _cap()
    async with tenant_scope(tid):
        vista = await cap.change_plan(sub_id, plan_id=str(bajo.id), plan=planes.snapshot(bajo))
    assert vista["cambio"] == "programado"
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 2, "hasta el corte sigue el descuento"

    await _correr_cron(tid, sub_id)  # el cron aplica el cambio programado

    sub = await _sub(tid, sub_id)
    assert sub.plan_id == bajo.id and sub.promocion_id is None
    assert await _estados_canje(tid, promo.id) == ["terminado"]
    [c2] = await _periodos(tid, sub_id)
    assert c2.monto == Decimal("50000") and c2.monto_bruto is None


async def test_un_downgrade_con_codigo_responde_promo_no_aplica_a_downgrade() -> None:
    from pack_subscriptions import planes

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    bajo = await _plan(tid, "BAJO", "50000")
    otra = await _promo(tid, "otra", valor="10", periodos=2)
    cap = await _cap()
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as e:
            await cap.change_plan(sub_id, plan_id=str(bajo.id), plan=planes.snapshot(bajo), codigo_promocion="otra")
    assert e.value.code == "promo_no_aplica_a_downgrade"
    assert (await _canjes(tid, otra.id)) == []


# ============================================================================= upgrade con promoción
async def _suscripcion_pagada_a_lista(precio: str = "100000"):
    tid = await _negocio()
    await _plan(tid, "BASIC", precio)
    pro = await _plan(tid, "PRO", "300000")
    cliente = await _cliente(tid)
    vista = await _inscribir(tid, cliente, "BASIC")
    sub_id = uuid.UUID(vista["subscription_id"])
    [cargo] = await _cargos(tid, sub_id)
    await _pago(tid, cargo.id, float(cargo.monto))
    return tid, pro, sub_id


async def test_upgrade_con_codigo_descuenta_la_diferencia_y_los_n_periodos_empiezan_en_el_siguiente_cargo() -> None:
    from pack_subscriptions import planes

    tid, pro, sub_id = await _suscripcion_pagada_a_lista()
    promo = await _promo(tid, valor="50", periodos=3)
    cap = await _cap()
    async with tenant_scope(tid):
        cotiza = await cap.cotizar_cambio(sub_id, plan_id=str(pro.id), plan=planes.snapshot(pro), codigo_promocion=CODIGO)
        assert cotiza["monto_bruto"] > cotiza["monto_a_pagar"] > 0
        assert cotiza["descuento"] == cotiza["monto_bruto"] - cotiza["monto_a_pagar"]
        vista = await cap.change_plan(
            sub_id, plan_id=str(pro.id), plan=planes.snapshot(pro),
            monto_esperado=cotiza["monto_a_pagar"], codigo_promocion=CODIGO,
        )
    cargo = next(c for c in await _cargos(tid, sub_id) if c.tipo == "cambio_plan")
    assert vista["cargo_cambio"]["monto"] == float(cargo.monto) == cotiza["monto_a_pagar"]
    assert (cargo.consume_periodo, cargo.periodo_n) == (False, None), "la diferencia no consume período"
    assert await _estados_canje(tid, promo.id) == ["reservado"]

    await _pago(tid, cargo.id, float(cargo.monto))

    sub = await _sub(tid, sub_id)
    assert sub.plan_id == pro.id and sub.descuento_periodos_restantes == 3
    assert await _estados_canje(tid, promo.id) == ["confirmado"]
    await _correr_cron(tid, sub_id)
    [c2] = await _periodos(tid, sub_id)
    assert (c2.monto, c2.monto_bruto, c2.periodo_n) == (Decimal("150000"), Decimal("300000"), 1)
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 2


async def test_un_codigo_nuevo_en_un_upgrade_reemplaza_el_descuento_vigente_solo_al_pagar() -> None:
    from pack_subscriptions import planes

    tid, _, promo_a, _, sub_id = await _activa_con_descuento(periodos=3)
    pro = await _dos_planes(tid)
    promo_b = await _promo(tid, "nueva", valor="20", periodos=2, etiqueta="Nueva")
    cap = await _cap()
    async with tenant_scope(tid):
        cotiza = await cap.cotizar_cambio(sub_id, plan_id=str(pro.id), plan=planes.snapshot(pro), codigo_promocion="nueva")
        assert cotiza["reemplaza_descuento"] == {"periodos_restantes": 2, "etiqueta": "Lanzamiento"}
        await cap.change_plan(
            sub_id, plan_id=str(pro.id), plan=planes.snapshot(pro),
            monto_esperado=cotiza["monto_a_pagar"], codigo_promocion="nueva",
        )
    # Sin pagar, conserva el anterior.
    sub = await _sub(tid, sub_id)
    assert sub.promocion_id == promo_a.id and sub.descuento_periodos_restantes == 2
    cargo = next(c for c in await _cargos(tid, sub_id) if c.tipo == "cambio_plan")

    await _pago(tid, cargo.id, float(cargo.monto))

    sub = await _sub(tid, sub_id)
    assert sub.promocion_id == promo_b.id and sub.descuento_periodos_restantes == 2
    assert await _estados_canje(tid, promo_a.id) == ["terminado"]
    assert await _estados_canje(tid, promo_b.id) == ["confirmado"]


async def test_un_upgrade_cuyo_codigo_no_admite_el_precio_completo_se_rechaza() -> None:
    from pack_subscriptions import planes

    tid, pro, sub_id = await _suscripcion_pagada_a_lista()
    await _promo(tid, valor="400000", tipo="monto", etiqueta="Descuentazo")
    cap = await _cap()
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as e:
            await cap.change_plan(sub_id, plan_id=str(pro.id), plan=planes.snapshot(pro), codigo_promocion=CODIGO)
    assert e.value.code == "descuento_excede_precio"


async def test_la_diferencia_usa_el_factor_del_periodo_pagado_con_descuento_aunque_ya_no_haya_descuento() -> None:
    from pack_subscriptions import planes
    from pack_subscriptions.prorrateo import diferencia_prorrateada

    tid, _, promo, _, sub_id = await _activa_con_descuento(periodos=1)  # 50 %, un solo período: ya agotado
    assert (await _sub(tid, sub_id)).promocion_id is None
    pro = await _dos_planes(tid)
    sub = await _sub(tid, sub_id)
    cap = await _cap()
    async with tenant_scope(tid):
        cotiza = await cap.cotizar_cambio(sub_id, plan_id=str(pro.id), plan=planes.snapshot(pro))
    completa = diferencia_prorrateada(
        Decimal(sub.valor_periodo), Decimal("300000"), sub.periodo_actual_inicio, sub.proximo_cargo,
        datetime.now(UTC), sub.moneda,
    )
    assert cotiza["monto_a_pagar"] == float((completa / 2).quantize(Decimal("1")))
    assert await _estados_canje(tid, promo.id) == ["agotado"]


# ============================================================================= invariante y esquema
async def test_el_invariante_cuadra_en_cada_paso_y_se_detecta_cuando_no() -> None:
    from pack_subscriptions import promociones

    from core.db.session import get_session

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=4)

    async def malas() -> list:
        async with tenant_scope(tid), get_session() as session:
            return await promociones.descuentos_inconsistentes(session)

    assert await malas() == []
    await _correr_cron(tid, sub_id)
    assert await malas() == []
    async with tenant_scope(tid), get_session() as session:  # se corrompe a mano: sobran períodos
        await session.execute(
            text("update pack_subscriptions_subscriptions set descuento_periodos_restantes = 3 where id = :i"),
            {"i": sub_id},
        )
    assert await malas() == [sub_id]


async def test_el_cron_diario_reporta_las_inconsistencias(caplog) -> None:
    from pack_subscriptions.jobs import _verificar_descuentos

    from core.db.session import get_session

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=4)
    async with tenant_scope(tid), get_session() as session:
        await session.execute(
            text("update pack_subscriptions_subscriptions set descuento_periodos_restantes = 1 where id = :i"),
            {"i": sub_id},
        )
    assert await _verificar_descuentos() >= 1


@pytest.mark.parametrize(
    "sentencia",
    [
        # las tres columnas del descuento van juntas
        "update pack_subscriptions_subscriptions set descuento_periodos_restantes = null where id = :i",
        # restantes entre 1 y periodos_total
        "update pack_subscriptions_subscriptions set descuento_periodos_restantes = 0 where id = :i",
        "update pack_subscriptions_subscriptions set descuento_periodos_restantes = 9 where id = :i",
    ],
)
async def test_la_base_rechaza_un_descuento_incoherente(sentencia) -> None:
    from core.db.session import get_session

    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=3)
    with pytest.raises(IntegrityError):
        async with tenant_scope(tid), get_session() as session:
            await session.execute(text(sentencia), {"i": sub_id})


# ============================================================================= Free → pago y cambio inmediato
async def test_tras_un_cambio_desde_free_pagado_el_link_del_periodo_no_cobra_otra_vez_ese_periodo() -> None:
    tid = await _negocio()
    await _plan(tid, "FREE", "0", trial_dias=14)
    await _plan(tid, "BASIC", "100000")
    promo = await _promo(tid, valor="50", periodos=3)
    cliente = await _cliente(tid)
    free = await _inscribir(tid, cliente, "FREE")
    sub_id = uuid.UUID(free["subscription_id"])
    await _con_codigo(tid, cliente)
    cargo = next(c for c in await _cargos(tid, sub_id) if c.tipo == "cambio_plan")
    await _pago(tid, cargo.id, float(cargo.monto))
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 2

    cap = await _cap()
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as e:
            await cap.charge_link(sub_id)
    assert e.value.code == "ya_pagado"
    assert await _periodos(tid, sub_id) == []
    assert (await _sub(tid, sub_id)).descuento_periodos_restantes == 2

    # El período siguiente sí sale con descuento: 2 de 3.
    await _correr_cron(tid, sub_id)
    [p2] = await _periodos(tid, sub_id)
    assert (p2.monto, p2.monto_bruto, p2.periodo_n) == (Decimal("50000.00"), Decimal("100000.00"), 2)
    assert promo.id is not None


async def test_un_cambio_inmediato_termina_el_descuento_vigente() -> None:
    from pack_subscriptions import planes

    tid, _, promo, _, sub_id = await _activa_con_descuento(periodos=3)
    gemelo = await _plan(tid, "GEMELO", "100000")  # mismo precio: el cambio es inmediato
    cap = await _cap()
    async with tenant_scope(tid):
        vista = await cap.change_plan(sub_id, plan_id=str(gemelo.id), plan=planes.snapshot(gemelo))
    assert vista["cambio"] == "inmediato"
    sub = await _sub(tid, sub_id)
    assert sub.plan_id == gemelo.id and sub.promocion_id is None and sub.descuento_periodos_restantes is None
    assert await _estados_canje(tid, promo.id) == ["terminado"]
