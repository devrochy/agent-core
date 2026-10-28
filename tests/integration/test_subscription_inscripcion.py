"""SPEC-43 · planes e inscripción en suscripciones (HU-SUB-27/28/30), contra la BD real.

Primero lo que solo se ve con concurrencia (un cliente que se inscribe dos veces a la vez, el pago que
llega junto con la expiración, el webhook repetido); después el ciclo de la inscripción (Free, de pago,
cambio desde Free, confirmación manual), el blindaje de `pendiente_pago` en las métricas y el aislamiento
entre negocios (RLS y FK compuesta).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _api_helpers import uniq
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

DATOS = {"nombre": "Ana Gómez", "email": "ana@ejemplo.co", "telefono": "+573001112233", "negocio": "Taller Ana"}


# ----------------------------------------------------------------------------- ayudas
async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("insc")
    tid, _ = await onboard_tenant(
        name=f"Insc {slug}", slug=slug, admin_email="admin@local", packs=["pack_subscriptions"], sembrar_ejemplos=False
    )
    return tid


async def _cliente(tid: uuid.UUID, nombre: str = "Ana") -> uuid.UUID:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('c')}", name=nombre,
                contact_phone=f"+57300{uuid.uuid4().int % 10_000_000:07d}",
            )
            session.add(cliente)
            await session.flush()
            return cliente.id


async def _plan(tid: uuid.UUID, codigo: str = "BASIC", precio: str = "99000", **extra):
    from pack_subscriptions.planes import DatosDePlan, crear

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await crear(
            DatosDePlan(
                codigo=codigo, nombre=extra.pop("nombre", codigo.title()), precio=Decimal(precio),
                periodicidad=extra.pop("periodicidad", "mensual"), **extra,
            )
        )


async def _inscribir(tid: uuid.UUID, cliente: uuid.UUID, codigo: str, **kw):
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.shared.tenant import tenant_scope

    kw.setdefault("origen", "panel")
    kw.setdefault("datos_alta", DATOS)
    async with tenant_scope(tid):
        return await SubscriptionCapabilityImpl().inscribir(customer_id=cliente, plan_code=codigo, **kw)


async def _sub(tid: uuid.UUID, sub_id):
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            sub = (await session.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
            session.expunge_all()
            return sub


async def _cargos(tid: uuid.UUID, sub_id):
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(SubscriptionCharge)
                    .where(SubscriptionCharge.subscription_id == sub_id)
                    .order_by(SubscriptionCharge.created_at)
                )
            ).scalars().all()
            session.expunge_all()
            return list(filas)


async def _eventos(tid: uuid.UUID, topic: str, sub_id=None) -> list:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(DomainEvent).where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic)
                )
            ).scalars().all()
    return [e for e in filas if sub_id is None or e.payload.get("subscription_id") == str(sub_id)]


async def _auditoria(tid: uuid.UUID, accion: str) -> list:
    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return list(
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == accion)
                    )
                ).scalars().all()
            )


async def _pago(tid: uuid.UUID, cargo_id, monto: float) -> None:
    """El webhook de la pasarela (`payment.confirmed` de la referencia `sub-<cargo>`)."""
    from pack_subscriptions.module import SubscriptionsModule

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await SubscriptionsModule()._on_payment_confirmed(f"sub-{cargo_id}", "wompi", f"ref-{uuid.uuid4().hex[:6]}", monto)


async def _pendiente(tid: uuid.UUID, codigo: str = "BASIC", precio: str = "99000"):
    """Cliente nuevo inscrito en un plan de pago: (cliente, vista, suscripción, cargo)."""
    cliente = await _cliente(tid)
    if codigo == "BASIC":
        from pack_subscriptions import planes

        from core.shared.tenant import tenant_scope

        async with tenant_scope(tid):
            existe = await planes.por_codigo(codigo)
        if existe is None:
            await _plan(tid, codigo, precio)
    vista = await _inscribir(tid, cliente, codigo)
    sub = await _sub(tid, uuid.UUID(vista["subscription_id"]))
    cargo = next(c for c in await _cargos(tid, sub.id) if c.tipo == "inscripcion")
    return cliente, vista, sub, cargo


# ============================================================================= concurrencia e idempotencia
async def test_inscripcion_repetida_a_la_vez_deja_una_sola_suscripcion_y_un_cargo() -> None:
    """Ocho inscripciones simultáneas del mismo cliente al mismo plan: una suscripción, un cargo, un número."""
    tid = await _negocio()
    await _plan(tid)
    cliente = await _cliente(tid)

    vistas = await asyncio.gather(*[_inscribir(tid, cliente, "BASIC") for _ in range(8)])

    assert len({v["subscription_id"] for v in vistas}) == 1
    assert len({v["numero_publico"] for v in vistas}) == 1
    assert sorted(v["inscripcion"] for v in vistas).count("nueva") == 1
    sub = await _sub(tid, uuid.UUID(vistas[0]["subscription_id"]))
    assert sub.estado == "pendiente_pago"
    cargos = await _cargos(tid, sub.id)
    assert [c.tipo for c in cargos] == ["inscripcion"]
    assert cargos[0].estado == "pendiente"


async def test_numeros_publicos_unicos_y_consecutivos_con_inscripciones_simultaneas() -> None:
    """Diez clientes a la vez en el mismo negocio: SUB-0001…SUB-0010, sin repetidos ni huecos."""
    tid = await _negocio()
    await _plan(tid)
    clientes = [await _cliente(tid, f"C{i}") for i in range(10)]

    vistas = await asyncio.gather(*[_inscribir(tid, c, "BASIC") for c in clientes])

    numeros = sorted(v["numero_publico"] for v in vistas)
    assert numeros == [f"SUB-{i:04d}" for i in range(1, 11)]


async def test_replay_secuencial_devuelve_la_misma_inscripcion_sin_duplicar() -> None:
    tid = await _negocio()
    await _plan(tid)
    cliente = await _cliente(tid)
    primera = await _inscribir(tid, cliente, "BASIC")
    segunda = await _inscribir(tid, cliente, "BASIC")
    assert primera["inscripcion"] == "nueva"
    assert segunda["inscripcion"] == "repetida"
    assert segunda["subscription_id"] == primera["subscription_id"]
    assert segunda["numero_publico"] == primera["numero_publico"]
    assert len(await _cargos(tid, uuid.UUID(primera["subscription_id"]))) == 1


async def test_webhook_repetido_activa_una_vez_y_emite_un_solo_evento() -> None:
    """Dos entregas del mismo `payment.confirmed` a la vez: una activación, un `subscription.activated`."""
    tid = await _negocio()
    _, _, sub, cargo = await _pendiente(tid)

    await asyncio.gather(*[_pago(tid, cargo.id, 99000.0) for _ in range(5)])

    sub = await _sub(tid, sub.id)
    assert sub.estado == "activa"
    eventos = await _eventos(tid, "subscription.activated", sub.id)
    assert len(eventos) == 1
    assert eventos[0].dedupe_key == f"sub_activated:{sub.id}:alta"
    assert eventos[0].payload["cambio_de_plan"] is False
    assert eventos[0].payload["referencia"] == sub.numero_publico
    assert eventos[0].payload["plan_id"] == str(sub.plan_id)
    assert (await _cargos(tid, sub.id))[0].estado == "pagado"


@pytest.mark.parametrize("ronda", range(4))
async def test_expiracion_y_pago_simultaneos_no_dejan_un_estado_mezclado(ronda: int) -> None:
    """El cron expira justo cuando llega el pago: gana quien toma el lock, y el resultado es coherente.

    O bien activa y el cargo queda pagado, o bien expira y el cargo queda anulado; nunca una
    suscripción cancelada con el cargo pagado y activada a la vez, ni dos eventos."""
    from pack_subscriptions.inscripcion import expirar_una

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    _, _, sub, cargo = await _pendiente(tid)

    async def expirar() -> bool:
        async with tenant_scope(tid):
            return await expirar_una(sub.id, limite=datetime.now(UTC) + timedelta(minutes=1))

    expiro, _ = await asyncio.gather(expirar(), _pago(tid, cargo.id, 99000.0))

    sub = await _sub(tid, sub.id)
    cargo_final = (await _cargos(tid, sub.id))[0]
    eventos = await _eventos(tid, "subscription.activated", sub.id)
    if sub.estado == "activa":
        assert expiro is False
        assert cargo_final.estado == "pagado"
        assert len(eventos) == 1
    else:
        assert expiro is True
        assert sub.estado == "cancelada"
        assert sub.motivo_cancelacion == "pago_no_recibido"
        assert cargo_final.estado == "anulado"
        assert eventos == []
        # El pago que llegó tarde no activó nada y se le avisó al negocio.
        assert await _auditoria(tid, "subs_pago_sin_aplicar")


# ============================================================================= ciclo de la inscripción
async def test_plan_de_pago_nace_pendiente_con_cargo_y_sin_periodo() -> None:
    tid = await _negocio()
    _, vista, sub, cargo = await _pendiente(tid)
    assert vista["estado"] == "pendiente_pago"
    assert vista["inscripcion"] == "nueva"
    assert vista["numero_publico"] == "SUB-0001"
    assert vista["cargo_inscripcion"]["monto"] == 99000.0
    assert sub.fecha_alta is None and sub.proximo_cargo is None and sub.periodo_actual_inicio is None
    assert cargo.estado == "pendiente" and Decimal(cargo.monto) == Decimal("99000")
    assert sub.datos_alta["email"] == "ana@ejemplo.co"
    assert sub.origen == "panel"
    assert await _eventos(tid, "subscription.activated", sub.id) == []


async def test_plan_free_nace_activo_sin_cargo_y_emite_activated_una_vez() -> None:
    tid = await _negocio()
    await _plan(tid, "FREE", "0", trial_dias=30)
    cliente = await _cliente(tid)
    primera = await _inscribir(tid, cliente, "FREE")
    sub = await _sub(tid, uuid.UUID(primera["subscription_id"]))
    assert sub.estado == "trialing" and sub.trial_termina is not None and sub.fecha_alta is not None
    assert await _cargos(tid, sub.id) == []
    eventos = await _eventos(tid, "subscription.activated", sub.id)
    assert len(eventos) == 1 and eventos[0].payload["cambio_de_plan"] is False
    # Repetir no crea otra: el cliente ya tiene este plan.
    from core.subscriptions import SubscriptionError

    with pytest.raises(SubscriptionError) as exc:
        await _inscribir(tid, cliente, "FREE")
    assert exc.value.code == "ya_tiene_este_plan"
    assert len(await _eventos(tid, "subscription.activated", sub.id)) == 1


async def test_free_que_contrata_un_plan_de_pago_es_un_cambio_sobre_la_misma_suscripcion() -> None:
    """D-SPEC-43-3: no nace una segunda suscripción (ni un segundo negocio en el alta automática)."""
    tid = await _negocio()
    plan_free = await _plan(tid, "FREE", "0", trial_dias=30)
    plan_basic = await _plan(tid, "BASIC", "99000")
    cliente = await _cliente(tid)
    free = await _inscribir(tid, cliente, "FREE")

    cambio = await _inscribir(tid, cliente, "BASIC")
    assert cambio["plan_id"] == str(plan_free.id)  # hasta que pague sigue en Free

    assert cambio["inscripcion"] == "cambio_de_plan"
    assert cambio["subscription_id"] == free["subscription_id"]
    sub = await _sub(tid, uuid.UUID(free["subscription_id"]))
    assert sub.estado == "trialing"  # sigue siendo Free hasta que pague
    cargo = next(c for c in await _cargos(tid, sub.id) if c.tipo == "cambio_plan")
    assert cargo.estado == "pendiente"

    await _pago(tid, cargo.id, float(cargo.monto))

    sub = await _sub(tid, sub.id)
    assert sub.estado == "activa" and sub.concepto == "Basic" and Decimal(sub.valor_periodo) == Decimal("99000")
    assert sub.plan_id == plan_basic.id  # al pagar apunta al plan destino, no al Free
    eventos = await _eventos(tid, "subscription.activated", sub.id)
    assert len(eventos) == 2
    cambio_evt = next(e for e in eventos if e.payload["cambio_de_plan"])
    assert cambio_evt.payload["plan_id"] == str(plan_basic.id)  # el alta automática resuelve el plan por aquí
    assert next(e for e in eventos if not e.payload["cambio_de_plan"]).payload["plan_id"] == str(plan_free.id)
    assert cambio_evt.dedupe_key == f"sub_activated:{sub.id}:cambio:{cargo.id}"
    assert cambio_evt.payload["referencia"] == sub.numero_publico


async def test_pago_con_monto_distinto_no_activa_y_avisa() -> None:
    tid = await _negocio()
    _, _, sub, cargo = await _pendiente(tid)
    await _pago(tid, cargo.id, 50000.0)
    assert (await _sub(tid, sub.id)).estado == "pendiente_pago"
    assert (await _cargos(tid, sub.id))[0].estado == "pendiente"
    assert await _auditoria(tid, "subs_pago_monto_distinto")
    assert await _eventos(tid, "subscription.activated", sub.id) == []
    # El pago correcto después sí activa.
    await _pago(tid, cargo.id, 99000.0)
    assert (await _sub(tid, sub.id)).estado == "activa"


async def test_pago_de_un_cargo_anulado_no_activa() -> None:
    """Cancelar la inscripción anula el cargo; si el cliente paga igual, no se activa y se avisa."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    _, _, sub, cargo = await _pendiente(tid)
    async with tenant_scope(tid):
        await SubscriptionCapabilityImpl().cancel(sub.id, reason="ya no", actor="test")
    assert (await _cargos(tid, sub.id))[0].estado == "anulado"

    await _pago(tid, cargo.id, 99000.0)

    sub = await _sub(tid, sub.id)
    assert sub.estado == "cancelada"
    assert await _eventos(tid, "subscription.activated", sub.id) == []
    assert await _auditoria(tid, "subs_pago_sin_aplicar")


async def test_cambiar_de_plan_pendiente_reemplaza_la_inscripcion_y_anula_su_cargo() -> None:
    tid = await _negocio()
    await _plan(tid, "BASIC", "99000")
    await _plan(tid, "PREMIUM", "199000")
    cliente = await _cliente(tid)
    a = await _inscribir(tid, cliente, "BASIC")
    b = await _inscribir(tid, cliente, "PREMIUM")
    assert a["subscription_id"] != b["subscription_id"]
    vieja = await _sub(tid, uuid.UUID(a["subscription_id"]))
    assert vieja.estado == "cancelada" and vieja.motivo_cancelacion == "reemplazada"
    assert (await _cargos(tid, vieja.id))[0].estado == "anulado"
    assert (await _sub(tid, uuid.UUID(b["subscription_id"]))).estado == "pendiente_pago"


async def test_confirmacion_manual_activa_por_el_mismo_camino_y_es_idempotente() -> None:
    from pack_subscriptions.inscripcion import confirmar_pago

    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError

    tid = await _negocio()
    _, _, sub, cargo = await _pendiente(tid)
    async with tenant_scope(tid):
        primera = await confirmar_pago(sub.id, referencia="COMPROBANTE-123", actor="admin:dueno@x.co")
        repetida = await confirmar_pago(sub.id, referencia="COMPROBANTE-123", actor="admin:dueno@x.co")
        with pytest.raises(SubscriptionError) as otra:
            await confirmar_pago(sub.id, referencia="OTRO-999", actor="admin:dueno@x.co")
        with pytest.raises(SubscriptionError) as corta:
            await confirmar_pago(sub.id, referencia="ab", actor="admin:dueno@x.co")

    assert primera["estado"] == "confirmado" and repetida["estado"] == "ya_confirmado"
    assert otra.value.code == "cargo_ya_pagado" and corta.value.code == "referencia_invalida"
    sub = await _sub(tid, sub.id)
    cargo = (await _cargos(tid, sub.id))[0]
    assert sub.estado == "activa" and cargo.estado == "pagado"
    assert cargo.pago_externo["canal"] == "transferencia" and cargo.pago_externo["referencia"] == "COMPROBANTE-123"
    assert len(await _eventos(tid, "subscription.activated", sub.id)) == 1
    assert len(await _auditoria(tid, "subs_pago_inscripcion_confirmado")) == 1


async def test_confirmacion_manual_y_webhook_a_la_vez_activan_una_vez() -> None:
    from pack_subscriptions.inscripcion import confirmar_pago

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    _, _, sub, cargo = await _pendiente(tid)

    async def manual() -> None:
        async with tenant_scope(tid):
            try:
                await confirmar_pago(sub.id, referencia="COMP-777", actor="admin:a@x.co")
            except Exception:
                pass

    await asyncio.gather(manual(), _pago(tid, cargo.id, 99000.0))

    assert (await _sub(tid, sub.id)).estado == "activa"
    assert len(await _eventos(tid, "subscription.activated", sub.id)) == 1


async def test_la_expiracion_cancela_solo_lo_vencido_y_es_idempotente() -> None:
    from pack_subscriptions.inscripcion import expirar_una

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    _, _, sub, _ = await _pendiente(tid)
    async with tenant_scope(tid):
        # Aún no vence: no hace nada.
        assert await expirar_una(sub.id, limite=datetime.now(UTC) - timedelta(days=1)) is False
        assert await expirar_una(sub.id, limite=datetime.now(UTC) + timedelta(minutes=1)) is True
        assert await expirar_una(sub.id, limite=datetime.now(UTC) + timedelta(minutes=1)) is False
    assert (await _sub(tid, sub.id)).estado == "cancelada"


# ============================================================================= pendiente_pago no es una suscripción viva
async def test_pendiente_pago_no_suma_a_mrr_altas_churn_ni_mora() -> None:
    from pack_subscriptions import consultas

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    await _pendiente(tid)  # una pendiente de pago
    async with tenant_scope(tid):
        async with get_session() as session:
            m = await consultas.metricas(session, tid, ahora=datetime.now(UTC) + timedelta(days=40))
    assert m.mrr == 0 and m.mrr_mes_anterior == 0
    assert m.altas_mes == 0 and m.altas_mes_anterior == 0
    assert m.activas == 0 and m.en_prueba == 0
    assert m.pendientes_de_pago == 1
    assert m.churn_mes is None
    assert m.mora.cargos == 0
    assert m.cohortes == []


async def test_al_pagar_entra_a_mrr_y_altas() -> None:
    from pack_subscriptions import consultas

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    _, _, _sub, cargo = await _pendiente(tid)
    await _pago(tid, cargo.id, 99000.0)
    async with tenant_scope(tid):
        async with get_session() as session:
            m = await consultas.metricas(session, tid)
    assert m.activas == 1 and m.pendientes_de_pago == 0
    assert round(m.mrr) == 99000
    assert m.altas_mes == 1


async def test_el_cron_de_cargos_y_de_vencidas_no_tocan_una_pendiente() -> None:
    from pack_subscriptions.jobs import _generar_cargos, _marcar_vencidas

    tid = await _negocio()
    _, _, sub, _ = await _pendiente(tid)
    futuro = datetime.now(UTC) + timedelta(days=90)
    await _generar_cargos(futuro)
    await _marcar_vencidas(futuro)
    assert (await _sub(tid, sub.id)).estado == "pendiente_pago"
    assert [c.tipo for c in await _cargos(tid, sub.id)] == ["inscripcion"]


# ============================================================================= aislamiento
async def test_un_negocio_no_ve_los_planes_ni_las_inscripciones_de_otro() -> None:
    from pack_subscriptions import planes
    from pack_subscriptions.models import Plan, Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, b = await _negocio(), await _negocio()
    await _plan(a, "SOLO-A", "10000")
    _, _, sub_a, _ = await _pendiente(a)

    async with tenant_scope(b):
        assert await planes.por_codigo("SOLO-A") is None
        assert await planes.listar(incluir_inactivos=True) == []
        async with get_session() as session:
            # Aun sin filtrar por tenant, la RLS forzada devuelve nada.
            assert (await session.execute(select(Plan))).scalars().all() == []
            assert (await session.execute(select(Subscription).where(Subscription.id == sub_a.id))).scalar_one_or_none() is None


async def test_la_fk_compuesta_impide_inscribir_en_el_plan_de_otro_negocio() -> None:
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, b = await _negocio(), await _negocio()
    plan_a = await _plan(a, "PLAN-A", "10000")
    cliente_b = await _cliente(b)
    with pytest.raises((IntegrityError, DBAPIError)):
        async with tenant_scope(b):
            async with get_session() as session:
                session.add(
                    Subscription(
                        tenant_id=b, customer_id=cliente_b, plan_id=plan_a.id, concepto="X",
                        valor_periodo=Decimal("1"), moneda="COP", periodicidad="mensual", estado="activa",
                    )
                )
                await session.flush()


async def test_la_confirmacion_y_el_pago_de_otro_negocio_no_aplican() -> None:
    """Confirmar a mano con el id de una inscripción de otro negocio responde «no existe»."""
    from pack_subscriptions.inscripcion import confirmar_pago

    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError

    a, b = await _negocio(), await _negocio()
    _, _, sub_a, cargo_a = await _pendiente(a)
    async with tenant_scope(b):
        with pytest.raises(SubscriptionError) as exc:
            await confirmar_pago(sub_a.id, referencia="COMP-000", actor="admin:b@x.co")
    assert exc.value.code == "suscripcion_no_encontrada"
    await _pago(b, cargo_a.id, 99000.0)  # el webhook de B con el cargo de A no lo encuentra
    assert (await _sub(a, sub_a.id)).estado == "pendiente_pago"


async def test_el_check_de_la_base_rechaza_una_pendiente_con_periodo() -> None:
    """Defensa en profundidad: aunque el código falle, el esquema no deja `pendiente_pago` con período."""
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    _, _, sub, _ = await _pendiente(tid)
    with pytest.raises((IntegrityError, DBAPIError)):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text("update pack_subscriptions_subscriptions set proximo_cargo = now() where id = :i"), {"i": sub.id}
                )


async def test_inscribir_rechaza_un_cliente_de_otro_negocio() -> None:
    from core.subscriptions import SubscriptionError

    a, b = await _negocio(), await _negocio()
    await _plan(a)
    cliente_b = await _cliente(b)
    with pytest.raises(SubscriptionError) as exc:
        await _inscribir(a, cliente_b, "BASIC")
    assert exc.value.code == "cliente_no_encontrado"
    with pytest.raises(SubscriptionError):
        await _inscribir(a, uuid.uuid4(), "BASIC")


async def test_el_contador_no_cruza_negocios_y_el_runtime_no_borra_planes_ni_contador() -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, b = await _negocio(), await _negocio()
    await _plan(a)
    async with tenant_scope(a):
        async with get_session() as session:
            with pytest.raises((IntegrityError, DBAPIError)):
                await session.execute(text("select pack_subscriptions_next_seq(:t)"), {"t": b})
    for tabla in ("pack_subscriptions_plans", "pack_subscriptions_counters"):
        with pytest.raises((IntegrityError, DBAPIError)):
            async with tenant_scope(a):
                async with get_session() as session:
                    await session.execute(text(f"delete from {tabla}"))
