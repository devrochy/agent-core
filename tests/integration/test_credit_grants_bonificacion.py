"""HU-FB-02 · el abono `bonificacion` contra el stack real (ADR-0213 §1-3, migración 0161).

- los CHECKs de 0161: la bonificación se admite sin `vence_en` y se rechaza con él;
- orden de consumo mes → acumulado → bonificación → recarga, leído de la base;
- al cierre, lo que queda de cada bonificación pasa al ciclo siguiente con su mismo `origen`, también con
  la suscripción cancelada (misma regla que la recarga);
- `acreditar_bonificacion` es idempotente dentro del ciclo y no abona sin ciclo;
- `GET /admin/plan` muestra «Regalos» (`bonuses`, `used_bonuses`) y los suma al total;
- la migración 0161 sube y baja, y no baja si ya hay bonificaciones.

Sin LLM ni red: las filas del ledger se insertan directo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.aislamiento,  # saldo y abonos por negocio (ADR-0209, ADR-0213)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

D = Decimal
RAIZ = Path(__file__).resolve().parents[2]
# Mismo calendario que `test_credit_grants_saldo.py`: ciclos [10-sep, 10-oct), [10-oct, 10-nov), …
INICIO = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
OCT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NOV = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
DIC = datetime(2026, 12, 10, 12, 0, tzinfo=UTC)
UNA_HORA = timedelta(hours=1)


@asynccontextmanager
async def _scope(tid: uuid.UUID) -> AsyncIterator[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


@asynccontextmanager
async def _owner() -> AsyncIterator[Any]:
    """Conexión del rol dueño (migraciones)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            yield conn
    finally:
        await engine.dispose()


async def _negocio(plan: str, *, period_start: datetime = INICIO, status: str = "active") -> uuid.UUID:
    from _habilidades import onboard_tenant

    from core.db.models import TenantSubscription

    slug = f"bo-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Regalos {slug}", slug=slug, sembrar_ejemplos=False, plan="free")
    async with _scope(tid) as s:
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
    return tid


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


async def _acreditar(tid: uuid.UUID, origen: str, creditos: int, now: datetime) -> str:
    from core.billing.bonificacion import acreditar_bonificacion

    async with _scope(tid) as s:
        return await acreditar_bonificacion(s, tid, origen, creditos, now=now)


# ------------------------------------------------------------------ CHECKs de 0161
async def test_la_bonificacion_se_admite_sin_vencimiento_y_no_con_el() -> None:
    tid = await _negocio("basic")
    await _abono(tid, OCT, "bonificacion", "500", origen="feedback:1")
    with pytest.raises(DBAPIError, match="ck_credit_grants_vence_en"):
        await _abono(tid, OCT, "bonificacion", "5", origen="feedback:2", vence_en=NOV)
    with pytest.raises(DBAPIError, match="ck_credit_grants_tipo"):
        await _abono(tid, OCT, "regalo", "5", origen="feedback:3")
    # Los otros dos tipos siguen con su regla.
    with pytest.raises(DBAPIError, match="ck_credit_grants_vence_en"):
        await _abono(tid, OCT, "recarga", "5", origen="r", vence_en=NOV)
    with pytest.raises(DBAPIError, match="ck_credit_grants_vence_en"):
        await _abono(tid, OCT, "acumulado", "5", origen="a", vence_en=OCT)
    assert await _abonos(tid) == [(OCT, "bonificacion", D("500.0"), "feedback:1", None)]


# ------------------------------------------------------------------ orden de consumo con los tres tipos
async def test_se_consume_mes_acumulado_bonificacion_y_al_final_la_recarga() -> None:
    tid = await _negocio("basic")  # 12.000 al mes
    await _abono(tid, OCT, "recarga", "1000", origen="pago-1")
    await _abono(tid, OCT, "bonificacion", "2000", origen="feedback:a")
    await _abono(tid, OCT, "acumulado", "500", origen="previo", vence_en=DIC)
    await _consumo(tid, "13500", OCT + timedelta(days=1))
    saldo = await _saldo(tid, OCT + timedelta(days=2))
    assert saldo.limite == D(15_500)
    d = saldo.desglose
    assert (d.usado_mes, d.usado_acumulado, d.usado_bonificaciones, d.usado_recargas) == (
        D(12_000), D(500), D(1_000), D(0),
    )
    assert saldo.disponible == D(2_000)


# ------------------------------------------------------------------ arrastre al cierre
async def test_la_bonificacion_se_arrastra_con_su_origen_y_al_cancelar_sigue_la_regla_de_la_recarga() -> None:
    from core.db.models import TenantSubscription

    tid = await _negocio("basic")
    recarga = await _abono(tid, INICIO, "recarga", "1000")
    await _abono(tid, INICIO, "bonificacion", "3000", origen="feedback:r1")
    await _consumo(tid, "13000", INICIO + timedelta(days=2))  # 12.000 del mes + 1.000 de la bonificación
    await _cerrar(OCT + UNA_HORA)
    assert await _abonos(tid, OCT) == [
        (OCT, "bonificacion", D("2000.0"), "feedback:r1", None),
        (OCT, "recarga", D("1000.0"), recarga, None),
    ]
    # Se cancela: el cierre sigue arrastrando la bonificación igual que la recarga.
    async with _scope(tid) as s:
        await s.execute(
            TenantSubscription.__table__.update()
            .where(TenantSubscription.tenant_id == tid)
            .values(status="canceled", canceled_at=OCT + timedelta(days=2))
        )
    await _cerrar(NOV + UNA_HORA)
    assert await _abonos(tid, NOV) == [
        (NOV, "bonificacion", D("2000.0"), "feedback:r1", None),
        (NOV, "recarga", D("1000.0"), recarga, None),
    ]
    # Reintentar el cierre no duplica.
    await _cerrar(NOV + 2 * UNA_HORA)
    assert len(await _abonos(tid, NOV)) == 2


async def test_una_bonificacion_gastada_entera_no_se_arrastra() -> None:
    tid = await _negocio("basic")
    await _abono(tid, INICIO, "bonificacion", "800", origen="feedback:g")
    await _consumo(tid, "12800", INICIO + timedelta(days=1))
    await _cerrar(OCT + UNA_HORA)
    assert await _abonos(tid, OCT) == []


# ------------------------------------------------------------------ `acreditar_bonificacion`
async def test_acreditar_bonificacion_es_idempotente_en_el_ciclo() -> None:
    tid = await _negocio("free", period_start=OCT)
    reporte = f"feedback:{uuid.uuid4()}"
    ahora = OCT + timedelta(days=5)
    assert await _acreditar(tid, reporte, 5_000, ahora) == "acreditada"
    assert await _acreditar(tid, reporte, 5_000, ahora + UNA_HORA) == "ya_acreditada"
    assert await _acreditar(tid, f"feedback:{uuid.uuid4()}", 1_000, ahora) == "acreditada"
    filas = await _abonos(tid)
    # El ciclo es el inicio de la ventana actual, no el instante de la aprobación.
    assert [(f[0], f[1], f[2]) for f in filas] == [(OCT, "bonificacion", D("1000.0")), (OCT, "bonificacion", D("5000.0"))]
    assert (await _saldo(tid, ahora)).limite == D(1_500 + 6_000)  # Free: 1.500 al mes + 6.000 de regalo


async def test_acreditar_bonificacion_sin_ciclo_no_abona() -> None:
    from core.db.models import TenantSubscription

    # `period_start` es NOT NULL: sin ciclo es sin suscripción viva.
    tid = await _negocio("free")
    async with _scope(tid) as s:
        await s.execute(
            TenantSubscription.__table__.update()
            .where(TenantSubscription.tenant_id == tid)
            .values(deleted_at=datetime.now(UTC))
        )
    assert await _acreditar(tid, "feedback:sin-ciclo", 100, OCT) == "sin_ciclo"
    assert await _abonos(tid) == []


# ------------------------------------------------------------------ GET /admin/plan
async def test_admin_plan_muestra_los_regalos(app_client) -> None:
    from _api_helpers import api_headers

    inicio = datetime.now(UTC) - timedelta(days=3)
    tid = await _negocio("basic", period_start=inicio)
    await _abono(tid, inicio, "bonificacion", "2000", origen="feedback:plan")
    await _abono(tid, inicio, "recarga", "1000")
    await _consumo(tid, "12500", inicio + timedelta(hours=1))

    r = await app_client.get("/api/v1/admin/plan", headers=await api_headers(app_client, tid))
    assert r.status_code == 200, r.text
    balance = r.json()["credit"]["balance"]
    assert (balance["total"], balance["bonuses"], balance["used_bonuses"]) == (15_000.0, 2_000.0, 500.0)
    assert (balance["top_ups"], balance["used_top_ups"]) == (1_000.0, 0.0)
    assert r.json()["credit"]["balance_percent"] == 83.3


# ------------------------------------------------------------------ migración 0161 arriba y abajo
def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=RAIZ, capture_output=True, text=True, timeout=300, check=False
    )


async def _definiciones() -> dict[str, str]:
    async with _owner() as conn:
        filas = (
            await conn.execute(
                text(
                    "select conname, pg_get_constraintdef(oid) from pg_constraint "
                    "where conrelid = 'credit_grants'::regclass and conname in "
                    "('ck_credit_grants_tipo', 'ck_credit_grants_vence_en')"
                )
            )
        ).all()
    return {n: d for n, d in filas}


async def _borrar_bonificaciones() -> None:
    """Como dueño y con el trigger de solo inserción apagado un instante: deja la base sin bonificaciones
    para que la 0161 pueda bajar (otros tests de este archivo las insertan)."""
    async with _owner() as conn:
        await conn.execute(text("ALTER TABLE credit_grants DISABLE TRIGGER credit_grants_inmutable"))
        await conn.execute(text("DELETE FROM credit_grants WHERE tipo = 'bonificacion'"))
        await conn.execute(text("ALTER TABLE credit_grants ENABLE TRIGGER credit_grants_inmutable"))


async def test_la_migracion_0161_sube_y_baja() -> None:
    antes = await _definiciones()
    assert all("bonificacion" in d for d in antes.values()), antes
    tid = await _negocio("basic")
    await _abono(tid, OCT, "bonificacion", "10", origen="feedback:migracion")
    try:
        # Con bonificaciones la reversa falla con un mensaje claro y no cambia nada.
        bajada = _alembic("downgrade", "0160")
        assert bajada.returncode != 0
        assert "no se puede revertir" in bajada.stderr
        assert await _definiciones() == antes
        await _borrar_bonificaciones()
        bajada = _alembic("downgrade", "0160")
        assert bajada.returncode == 0, bajada.stderr[-2000:]
        assert not any("bonificacion" in d for d in (await _definiciones()).values())
    finally:
        subida = _alembic("upgrade", "head")
        assert subida.returncode == 0, subida.stderr[-2000:]
    assert await _definiciones() == antes
