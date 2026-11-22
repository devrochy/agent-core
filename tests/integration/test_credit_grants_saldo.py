"""HU-BILL-22 · `credit_grants`, saldo del ciclo y cierre contra el stack real (ADR-0209 §1-3).

- orden de consumo mes → acumulado → recarga, leído de la base (Gherkin 1), y saldo reproducible;
- acumulación al cierre: Premium 10.000 sobrantes con vencimiento a un ciclo, Empresarial a dos, Free y
  Basic nada (Gherkin 2);
- una recarga sigue contando ciclo tras ciclo; con la suscripción cancelada el negocio no opera pero el
  registro queda y se sigue arrastrando (Gherkin 3, negocio vencido);
- aislamiento: un negocio nunca ve ni escribe `credit_grants` de otro (RLS forzada y `tenant_id`, Gherkin 4);
- cierre idempotente, período reiniciado, trigger de solo inserción y `purge_tenant`.

Sin LLM ni red: las filas del ledger y los abonos se insertan directo (las recargas por Wompi son HU-BILL-23).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.aislamiento,  # saldo y abonos por negocio (ADR-0209)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

D = Decimal
# Calendario fijo: el período empezó el 10-sep a las 12:00 UTC (ancla 10). Ciclos: N-1 = [10-sep, 10-oct),
# N = [10-oct, 10-nov), N+1 = [10-nov, 10-dic). El cron corre «una hora después» de cada inicio.
INICIO = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
OCT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NOV = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
DIC = datetime(2026, 12, 10, 12, 0, tzinfo=UTC)
ENE = datetime(2027, 1, 10, 12, 0, tzinfo=UTC)
UNA_HORA = timedelta(hours=1)


@asynccontextmanager
async def _scope(tid: uuid.UUID) -> AsyncIterator[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


@asynccontextmanager
async def _owner(tid: uuid.UUID) -> AsyncIterator[Any]:
    """Conexión del rol dueño (migraciones): salta los GRANT, no el trigger ni la RLS forzada."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("select set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
            yield conn
    finally:
        await engine.dispose()


async def _negocio(plan: str, *, period_start: datetime = INICIO, status: str = "active") -> tuple[uuid.UUID, str]:
    """Negocio con su suscripción en `plan`, desde `period_start` (ancla = su día) y pagada hasta 2027."""
    from _habilidades import onboard_tenant

    from core.db.models import TenantSubscription

    slug = f"cg-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Abonos {slug}", slug=slug, sembrar_ejemplos=False, plan="free")
    async with _scope(tid) as s:
        sub = (
            await s.execute(
                TenantSubscription.__table__.select().where(TenantSubscription.tenant_id == tid)
            )
        ).first()
        assert sub is not None
        await s.execute(
            TenantSubscription.__table__.update()
            .where(TenantSubscription.tenant_id == tid)
            .values(
                plan_id=plan,
                status=status,
                period_start=period_start,
                period_end=datetime(2027, 6, 10, 12, 0, tzinfo=UTC),
                dia_ancla=period_start.day,
                trial_ends_at=None,
            )
        )
    return tid, slug


async def _consumo(tid: uuid.UUID, creditos: str, cuando: datetime) -> None:
    from core.db.models import UsageLedger

    async with _scope(tid) as s:
        s.add(
            UsageLedger(
                tenant_id=tid,
                kind="turn",
                model="claude-haiku-4-5",
                input_tokens=100,
                output_tokens=10,
                cost_usd=D("0.001"),
                credits=D(creditos),
                key_mode="platform",
                tarifario_version="v1",
                trm_cop=D("4000"),
                created_at=cuando,
            )
        )


async def _abono(tid: uuid.UUID, ciclo: datetime, tipo: str, creditos: str, *, origen: str | None = None,
                 vence_en: datetime | None = None) -> str:
    from core.db.models import CreditGrant

    origen = origen or str(uuid.uuid4())
    async with _scope(tid) as s:
        s.add(CreditGrant(tenant_id=tid, ciclo=ciclo, tipo=tipo, creditos=D(creditos), origen=origen, vence_en=vence_en))
    return origen


async def _abonos(tid: uuid.UUID, ciclo: datetime | None = None) -> list[tuple[Any, ...]]:
    async with _scope(tid) as s:
        filas = (
            await s.execute(
                text(
                    "select ciclo, tipo, creditos, origen, vence_en from credit_grants "
                    "where tenant_id = :t and (cast(:c as timestamptz) is null or ciclo = :c) order by ciclo, tipo, origen"
                ),
                {"t": tid, "c": ciclo},
            )
        ).all()
    return [tuple(f) for f in filas]


async def _saldo(tid: uuid.UUID, now: datetime) -> Any:
    from core.billing.creditos import _saldo_del_mes

    async with _scope(tid) as s:
        return await _saldo_del_mes(s, tid, now=now, con_lock=False)


async def _cerrar(now: datetime) -> dict[str, int]:
    from core.tasks.billing_lifecycle import cerrar_ciclos_de_credito

    return await cerrar_ciclos_de_credito(now=now)


# ------------------------------------------------------------------ Gherkin 1: orden de consumo
async def test_se_consume_primero_el_mes_luego_lo_acumulado_y_al_final_la_recarga() -> None:
    from core.billing.creditos import alcanza
    from core.billing.enforcement import QuotaExceeded

    tid, _ = await _negocio("premium")  # 40.000 al mes
    await _abono(tid, OCT, "acumulado", "1000", origen=OCT.isoformat(), vence_en=NOV)
    await _abono(tid, OCT, "recarga", "500")
    ahora = OCT + timedelta(days=5)

    await _consumo(tid, "39000", ahora - UNA_HORA)
    d = (await _saldo(tid, ahora)).desglose
    assert (d.usado_mes, d.usado_acumulado, d.usado_recargas) == (D(39_000), D(0), D(0))

    await _consumo(tid, "1500", ahora - UNA_HORA)
    d = (await _saldo(tid, ahora)).desglose
    assert (d.usado_mes, d.usado_acumulado, d.usado_recargas) == (D(40_000), D(500), D(0))

    await _consumo(tid, "800", ahora - UNA_HORA)
    saldo = await _saldo(tid, ahora)
    d = saldo.desglose
    assert (d.usado_mes, d.usado_acumulado, d.usado_recargas) == (D(40_000), D(1_000), D(300))
    assert (saldo.limite, saldo.disponible, d.acumulado_vence_en) == (D(41_500), D(200), None)

    # `alcanza` mide el mismo saldo (con el lock): caben 200, no 201.
    async with _scope(tid) as s:
        await alcanza(s, tid, 200, now=ahora)
    with pytest.raises(QuotaExceeded):
        async with _scope(tid) as s:
            await alcanza(s, tid, 201, now=ahora)


async def test_el_saldo_es_reproducible_al_leerlo_dos_veces() -> None:
    tid, _ = await _negocio("basic")
    await _abono(tid, OCT, "recarga", "2500.5")
    await _consumo(tid, "13000.2", OCT + timedelta(days=1))
    ahora = OCT + timedelta(days=2)
    uno, dos = await _saldo(tid, ahora), await _saldo(tid, ahora)
    assert uno == dos
    assert (uno.limite, uno.desglose.usado_recargas, uno.disponible) == (D("14500.5"), D("1000.2"), D("1500.3"))


# ------------------------------------------------------------------ Gherkin 2: acumulación por plan
@pytest.mark.parametrize(
    ("plan", "mensual", "vence_en"),
    [("premium", 40_000, NOV), ("empresarial", 120_000, DIC), ("basic", 12_000, None), ("free", 1_500, None)],
)
async def test_el_sobrante_del_mes_se_acumula_segun_el_plan(plan: str, mensual: int, vence_en: datetime | None) -> None:
    tid, _ = await _negocio(plan)
    # Premium y Empresarial dejan 10.000 sin usar; Basic y Free, la mitad: ninguno de los dos acumula.
    usado = mensual - 10_000 if mensual > 10_000 else mensual // 2
    await _consumo(tid, str(usado), INICIO + timedelta(days=3))
    await _cerrar(OCT + UNA_HORA)
    filas = await _abonos(tid)
    if vence_en is None:
        assert filas == []
        return
    assert filas == [(OCT, "acumulado", D("10000.0"), INICIO.isoformat(), vence_en)]
    saldo = await _saldo(tid, OCT + timedelta(days=1))
    assert (saldo.limite, saldo.desglose.acumulado_vence_en) == (D(mensual + 10_000), vence_en)


async def test_premium_pierde_el_acumulado_al_ciclo_siguiente_y_empresarial_lo_conserva_dos() -> None:
    premium, _ = await _negocio("premium")
    empresarial, _ = await _negocio("empresarial")
    for tid, mensual in ((premium, 40_000), (empresarial, 120_000)):
        await _consumo(tid, str(mensual - 10_000), INICIO + timedelta(days=3))
    await _cerrar(OCT + UNA_HORA)
    # En N se usa todo el mes y nada del acumulado: N+1 recibe solo lo que sigue vigente.
    await _consumo(premium, "40000", OCT + timedelta(days=3))
    await _consumo(empresarial, "120000", OCT + timedelta(days=3))
    await _cerrar(NOV + UNA_HORA)
    assert await _abonos(premium, NOV) == []
    assert await _abonos(empresarial, NOV) == [(NOV, "acumulado", D("10000.0"), INICIO.isoformat(), DIC)]
    await _consumo(empresarial, "120000", NOV + timedelta(days=3))
    await _cerrar(DIC + UNA_HORA)
    assert await _abonos(empresarial, DIC) == []  # venció en DIC: ya no pasa a [DIC, ENE)


# ------------------------------------------------------------------ Gherkin 3: recargas sin vencimiento
async def test_la_recarga_sigue_contando_ciclo_tras_ciclo_y_al_cancelar_deja_de_contar() -> None:
    from core.billing.enforcement import tenant_operativo
    from core.db.models import TenantSubscription

    tid, _ = await _negocio("basic")
    origen = await _abono(tid, INICIO, "recarga", "5000")
    await _consumo(tid, "13000", INICIO + timedelta(days=2))  # 12.000 del mes + 1.000 de la recarga
    await _cerrar(OCT + UNA_HORA)
    assert await _abonos(tid, OCT) == [(OCT, "recarga", D("4000.0"), origen, None)]
    await _consumo(tid, "12500", OCT + timedelta(days=2))
    await _cerrar(NOV + UNA_HORA)
    assert await _abonos(tid, NOV) == [(NOV, "recarga", D("3500.0"), origen, None)]
    assert (await _saldo(tid, NOV + timedelta(days=1))).limite == D(15_500)

    # Se cancela: el negocio deja de operar (no hay turnos que gasten el saldo), pero el registro queda
    # y el cierre lo sigue arrastrando para cuando vuelva.
    async with _scope(tid) as s:
        await s.execute(
            TenantSubscription.__table__.update()
            .where(TenantSubscription.tenant_id == tid)
            .values(status="canceled", canceled_at=NOV + timedelta(days=2))
        )
    assert await tenant_operativo(tid) is None
    await _cerrar(DIC + UNA_HORA)
    assert await _abonos(tid, DIC) == [(DIC, "recarga", D("3500.0"), origen, None)]
    assert len(await _abonos(tid)) == 4  # nada se borra: un registro por ciclo


async def test_un_negocio_vencido_conserva_las_recargas_y_lo_acumulado_sigue_venciendo() -> None:
    tid, _ = await _negocio("empresarial", status="expired")
    recarga = await _abono(tid, OCT, "recarga", "700")
    await _abono(tid, OCT, "acumulado", "900", origen="viejo", vence_en=NOV)
    await _abono(tid, OCT, "acumulado", "800", origen="vigente", vence_en=DIC)
    await _cerrar(NOV + UNA_HORA)
    # Vencido no acumula el sobrante del mes; la recarga y el acumulado vigente pasan, el que vence no.
    assert await _abonos(tid, NOV) == [
        (NOV, "acumulado", D("800.0"), "vigente", DIC),
        (NOV, "recarga", D("700.0"), recarga, None),
    ]


# ------------------------------------------------------------------ idempotencia y período reiniciado
async def test_el_cierre_es_idempotente() -> None:
    tid, _ = await _negocio("premium")
    await _abono(tid, INICIO, "recarga", "300", origen="pago-1")
    await _consumo(tid, "25000", INICIO + timedelta(days=1))
    await _cerrar(OCT + UNA_HORA)
    despues_de_uno = await _abonos(tid, OCT)
    await _cerrar(OCT + 2 * UNA_HORA)
    await _cerrar(OCT + timedelta(days=3))
    assert await _abonos(tid, OCT) == despues_de_uno
    assert despues_de_uno == [
        (OCT, "acumulado", D("15000.0"), INICIO.isoformat(), NOV),
        (OCT, "recarga", D("300.0"), "pago-1", None),
    ]
    # Pasada la gracia ya no se vuelve a medir el ciclo cerrado.
    from core.tasks.billing_lifecycle import GRACIA_DEL_CIERRE

    await _cerrar(OCT + GRACIA_DEL_CIERRE + UNA_HORA)
    assert await _abonos(tid, OCT) == despues_de_uno


async def test_al_reiniciar_el_periodo_se_cierra_la_ventana_abandonada() -> None:
    from core.billing.periodos import periodo_de, ventana_mensual
    from core.billing.service import aplicar_pago_de_periodo
    from core.db.models import TenantSubscription

    ahora = datetime.now(UTC)
    inicio_viejo = ahora - timedelta(days=75)
    tid, slug = await _negocio("premium", period_start=inicio_viejo, status="canceled")
    async with _scope(tid) as s:
        await s.execute(
            TenantSubscription.__table__.update()
            .where(TenantSubscription.tenant_id == tid)
            .values(period_end=ahora - timedelta(days=15))
        )
    ventana, _fin = ventana_mensual(inicio_viejo, inicio_viejo.day, ahora)
    origen = await _abono(tid, ventana, "recarga", "1200")
    await _consumo(tid, "200", ventana + timedelta(seconds=30))

    periodo = periodo_de(slug, ahora, ahora.day, desde_cero=True)
    async with _scope(tid) as s:
        await aplicar_pago_de_periodo(
            s, tid, plan_id="basic", periodo=periodo, provider="wompi",
            provider_reference=f"ref-{uuid.uuid4().hex[:8]}", now=ahora,
        )
    nueva, _ = ventana_mensual(periodo.inicio, ahora.day, ahora)
    assert nueva == periodo.inicio != ventana
    # Cancelada antes del pago: el mes no se acumula; la recarga pasa entera (el mes cubrió los 200).
    assert await _abonos(tid, nueva) == [(nueva, "recarga", D("1200.0"), origen, None)]
    saldo = await _saldo(tid, ahora + timedelta(minutes=1))
    assert saldo.limite == D(12_000 + 1_200)


# ------------------------------------------------------------------ Gherkin 4: aislamiento
async def test_un_negocio_nunca_ve_ni_escribe_los_abonos_de_otro() -> None:
    from core.billing.creditos import leer_abonos
    from core.db.models import CreditGrant

    a, _ = await _negocio("basic")
    b, _ = await _negocio("basic")
    await _abono(a, OCT, "recarga", "999")
    async with _scope(b) as s:
        # Sin filtro de tenant: la RLS forzada no deja ver nada de A.
        assert (await s.execute(text("select count(*) from credit_grants where tenant_id = :a"), {"a": a})).scalar() == 0
        assert await leer_abonos(s, b, OCT) == []
    assert (await _saldo(b, OCT + UNA_HORA)).limite == D(12_000)
    assert (await _saldo(a, OCT + UNA_HORA)).limite == D(12_999)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with _scope(b) as s:
            s.add(CreditGrant(tenant_id=a, ciclo=OCT, tipo="recarga", creditos=D(1), origen="intruso"))
    # Fuera de su scope, `leer_abonos` falla en vez de devolver 0 en silencio.
    with pytest.raises(RuntimeError, match="tenant_scope"):
        async with _scope(b) as s:
            await leer_abonos(s, a, OCT)


# ------------------------------------------------------------------ solo inserción y purga
async def test_los_abonos_son_solo_de_insercion() -> None:
    tid, _ = await _negocio("basic")
    await _abono(tid, OCT, "recarga", "10", origen="fijo")
    for sql in (
        "update credit_grants set creditos = 999 where origen = 'fijo'",
        "delete from credit_grants where origen = 'fijo'",
        "truncate credit_grants",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with _owner(tid) as conn:  # ni el dueño de la tabla puede
                await conn.execute(text(sql))
    for sql in ("update credit_grants set creditos = 999", "delete from credit_grants"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with _scope(tid) as s:
                await s.execute(text(sql))
    with pytest.raises(DBAPIError, match="ck_credit_grants"):
        await _abono(tid, OCT, "recarga", "5", vence_en=NOV)  # una recarga no vence
    with pytest.raises(DBAPIError, match="uq_credit_grants"):
        await _abono(tid, OCT, "recarga", "5", origen="fijo")
    assert await _abonos(tid) == [(OCT, "recarga", D("10.0"), "fijo", None)]


async def test_purge_tenant_borra_los_abonos_del_negocio() -> None:
    from core.tenants import deactivate_tenant, purge_tenant

    tid, slug = await _negocio("basic")
    await _abono(tid, OCT, "recarga", "10")
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")
    async with _owner(tid) as conn:
        assert (await conn.execute(text("select count(*) from credit_grants where tenant_id = :t"), {"t": tid})).scalar() == 0


# ------------------------------------------------------------------ GET /admin/plan
async def test_admin_plan_muestra_el_saldo_desglosado(app_client) -> None:
    from _api_helpers import api_headers

    inicio = datetime.now(UTC) - timedelta(days=3)
    tid, _ = await _negocio("premium", period_start=inicio)
    otro, _ = await _negocio("premium", period_start=inicio)
    vence = inicio + timedelta(days=40)
    await _abono(tid, inicio, "acumulado", "2000", origen="previo", vence_en=vence)
    await _abono(tid, inicio, "recarga", "1000.5")
    await _consumo(tid, "41000", inicio + timedelta(hours=1))

    r = await app_client.get("/api/v1/admin/plan", headers=await api_headers(app_client, tid))
    assert r.status_code == 200, r.text
    credito = r.json()["credit"]
    # Los campos de antes no cambian: `limit` y `percent` son del crédito mensual del plan.
    assert (credito["limit"], credito["used"], credito["percent"]) == (40_000, 41_000.0, 102.5)
    balance = credito["balance"]
    assert {k: v for k, v in balance.items() if k != "accumulated_expires_at"} == {
        "total": 43_000.5,
        "month": 40_000.0,
        "accumulated": 2_000.0,
        "top_ups": 1_000.5,
        "used_month": 40_000.0,
        "used_accumulated": 1_000.0,
        "used_top_ups": 0.0,
        "over_balance": 0.0,
    }
    assert datetime.fromisoformat(balance["accumulated_expires_at"]) == vence
    assert credito["balance_percent"] == 95.3

    r = await app_client.get("/api/v1/admin/plan", headers=await api_headers(app_client, otro))
    assert r.status_code == 200, r.text
    assert r.json()["credit"]["balance"]["total"] == 40_000.0  # nunca los abonos del otro negocio
