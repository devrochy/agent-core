"""HU-BILL-22 · saldo del ciclo con crédito del mes, acumulado y recargas (ADR-0209 §2-3), sin base.

- `repartir` gasta primero el mes, luego lo acumulado (el que vence antes) y al final las recargas; lo que
  pasa del saldo es colchón y no se carga a ninguna fuente.
- el vencimiento del sobrante avanza por calendario (`sumar_periodo` con día ancla): Premium 1 período,
  Empresarial 2; Free y Basic no acumulan, ni un plan pago cancelado.
- el colchón sigue siendo el 5 % del crédito MENSUAL (ADR-0208 §5) y se mide contra el saldo total: una
  recarga del mismo ciclo cubre primero lo consumido en el colchón.
- `_saldo_del_mes` suma los abonos del ciclo al límite y expone el desglose.

La integración contra Postgres (RLS, trigger, cierre real) está en
`tests/integration/test_credit_grants_saldo.py`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.cierre_de_ciclo import (
    meses_que_acumula,
    origen_del_ciclo,
    vencimiento_del_acumulado,
)
from core.billing.creditos import Abono, creditos_que_quedan, orden_de_gasto, repartir

D = Decimal
VENCE_1 = datetime(2026, 12, 10, tzinfo=UTC)
VENCE_2 = datetime(2027, 1, 10, tzinfo=UTC)


def _acum(creditos: str, vence: datetime, origen: str = "a") -> Abono:
    return Abono("acumulado", D(creditos), origen, vence)


def _recarga(creditos: str, origen: str = "r") -> Abono:
    return Abono("recarga", D(creditos), origen)


# ----------------------------------------------------------------------- orden de gasto (Gherkin 1)
@pytest.mark.parametrize(
    ("usado", "mes", "acumulado", "recargas", "colchon"),
    [
        ("0", "0", "0", "0", "0"),
        ("30000", "30000", "0", "0", "0"),
        ("40000", "40000", "0", "0", "0"),
        ("42500", "40000", "2500", "0", "0"),
        ("45000", "40000", "3000", "2000", "0"),
        ("46000", "40000", "3000", "3000", "0"),
        ("46200.5", "40000", "3000", "3000", "200.5"),
    ],
)
def test_se_gasta_mes_luego_acumulado_y_al_final_recargas(usado, mes, acumulado, recargas, colchon) -> None:
    reparto = repartir(D(usado), 40_000, [_recarga("3000"), _acum("3000", VENCE_1)])
    assert reparto.usado_mes == D(mes)
    assert reparto.usado_acumulado == D(acumulado)
    assert reparto.usado_recargas == D(recargas)
    assert reparto.sobre_el_saldo == D(colchon)
    assert reparto.total == D(46_000)
    assert reparto.usado_mes + reparto.usado_acumulado + reparto.usado_recargas + reparto.sobre_el_saldo == D(usado)


def test_entre_acumulados_se_gasta_primero_el_que_vence_antes() -> None:
    tarde, pronto = _acum("1000", VENCE_2, "viejo"), _acum("1000", VENCE_1, "nuevo")
    assert [a.origen for a in orden_de_gasto([_recarga("5"), tarde, pronto])] == ["nuevo", "viejo", "r"]
    reparto = repartir(D(1_500), 0, [tarde, pronto])
    usado = {u.abono.origen: u.usado for u in reparto.abonos}
    assert usado == {"nuevo": D(1000), "viejo": D(500)}
    # lo que queda vence en el más lejano: el próximo vencimiento es el de lo que todavía queda
    assert reparto.acumulado_vence_en == VENCE_2


def test_el_reparto_es_reproducible_con_cualquier_orden_de_lectura() -> None:
    abonos = [_recarga("10", "r2"), _acum("10", VENCE_1, "a1"), _recarga("10", "r1"), _acum("10", VENCE_1, "a0")]
    uno = repartir(D(35), 0, abonos)
    otro = repartir(D(35), 0, list(reversed(abonos)))
    assert uno == otro
    assert [(u.abono.origen, u.usado) for u in uno.abonos] == [
        ("a0", D(10)), ("a1", D(10)), ("r1", D(10)), ("r2", D(5)),
    ]


def test_lo_que_se_arrastra_se_trunca_a_la_decima_hacia_abajo() -> None:
    assert creditos_que_quedan(D("123.79")) == D("123.7")
    assert creditos_que_quedan(D("-4")) == D(0)


# --------------------------------------------------------------------- acumulación por plan (Gherkin 2)
@pytest.mark.parametrize(
    ("plan", "status", "meses"),
    [
        ("free", "active", 0),
        ("basic", "active", 0),
        ("premium", "active", 1),
        ("empresarial", "active", 2),
        ("premium", "past_due", 1),  # el ciclo cerró antes de pagar la renovación: igual acumula
        ("premium", "canceled", 0),
        ("empresarial", "expired", 0),
        ("no-existe", "active", 0),
    ],
)
def test_cuantos_periodos_acumula_cada_plan(plan, status, meses) -> None:
    assert meses_que_acumula(plan, status) == meses


def test_el_vencimiento_avanza_por_calendario_con_el_dia_ancla() -> None:
    fin = datetime(2027, 1, 31, 9, 30, tzinfo=UTC)
    assert vencimiento_del_acumulado(fin, 1, 31) == datetime(2027, 2, 28, 9, 30, tzinfo=UTC)
    assert vencimiento_del_acumulado(fin, 2, 31) == datetime(2027, 3, 31, 9, 30, tzinfo=UTC)
    assert vencimiento_del_acumulado(fin, 0, 31) == fin


def test_el_origen_del_acumulado_es_el_inicio_del_ciclo_en_utc() -> None:
    inicio = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert origen_del_ciclo(inicio) == "2026-10-10T12:00:00+00:00"


# ---------------------------------------------------- `_saldo_del_mes` y el colchón con recargas
@pytest.fixture
def ciclo(monkeypatch):  # type: ignore[no-untyped-def]
    from core.billing import creditos, enforcement

    estado: dict[str, Any] = {"usado": D(0), "abonos": [], "abierta": (True, datetime.now(UTC))}
    overview = SimpleNamespace(
        plan_id="basic", period_start=datetime.now(UTC) - timedelta(days=3), dia_ancla=None, status="active"
    )

    async def _sub(_tid):  # type: ignore[no-untyped-def]
        return overview

    async def _medir(_session, _tid, *, desde=None, hasta=None, **_k):  # type: ignore[no-untyped-def]
        return creditos.ConsumoDeCreditos(total=estado["usado"], por_concepto={})

    async def _abonos(_session, _tid, ciclo):  # type: ignore[no-untyped-def]
        estado["ciclo"] = ciclo
        return list(estado["abonos"])

    async def _abierta(_session, _tid, _cid):  # type: ignore[no-untyped-def]
        return estado["abierta"]

    @asynccontextmanager
    async def _get_session():  # type: ignore[no-untyped-def]
        yield SimpleNamespace()

    monkeypatch.setattr(enforcement, "subscription_overview", _sub)
    monkeypatch.setattr(creditos, "medir_creditos", _medir)
    monkeypatch.setattr(creditos, "leer_abonos", _abonos)
    monkeypatch.setattr(creditos, "conversacion_abierta", _abierta)
    monkeypatch.setattr("core.db.session.get_session", _get_session)
    return estado


async def _decidir() -> Any:
    from core.billing.creditos import decidir_turno
    from core.shared.tenant import tenant_scope

    tid = uuid.uuid4()
    async with tenant_scope(tid):
        return await decidir_turno(SimpleNamespace(), tid, uuid.uuid4())


async def test_el_saldo_suma_los_abonos_del_ciclo_y_expone_el_desglose(ciclo) -> None:
    ciclo["usado"] = D("12500")
    ciclo["abonos"] = [_recarga("1000")]
    decision = await _decidir()
    saldo = decision.saldo
    assert decision.salida == "normal"
    assert saldo.limite == D(13_000)
    assert saldo.credito_mensual == D(12_000)
    assert ciclo["ciclo"] == saldo.inicio  # los abonos se leen del ciclo = inicio de la ventana
    assert saldo.desglose is not None
    assert (saldo.desglose.usado_mes, saldo.desglose.usado_recargas) == (D(12_000), D(500))
    assert saldo.disponible == D(500)


async def test_el_colchon_es_del_credito_mensual_y_la_recarga_lo_cubre_primero(ciclo) -> None:
    # Sin recarga: 12.300 de 12.000 → en el colchón (300 de 600).
    ciclo["usado"] = D("12300")
    sin = await _decidir()
    assert (sin.salida, sin.colchon_usado, sin.colchon_limite) == ("colchon", D(300), D(600))
    # Recarga de 1.000 en el mismo ciclo: cubre primero los 300 del colchón y el turno vuelve a `normal`;
    # el tamaño del colchón no crece con la recarga (5 % de 12.000, no de 13.000).
    ciclo["abonos"] = [_recarga("1000")]
    con = await _decidir()
    assert (con.salida, con.colchon_usado, con.colchon_limite) == ("normal", D(0), D(600))
    # Agotada también la recarga: el colchón se mide sobre el saldo total.
    ciclo["usado"] = D("13700")
    tope = await _decidir()
    assert (tope.salida, tope.colchon_usado, tope.colchon_limite) == ("agotado", D(700), D(600))


async def test_alcanza_cuenta_las_recargas_y_reporta_el_limite_entero(ciclo) -> None:
    from core.billing.creditos import alcanza
    from core.billing.enforcement import QuotaExceeded
    from core.shared.tenant import tenant_scope

    class _Sesion:
        async def execute(self, *_a, **_k):  # type: ignore[no-untyped-def]
            return None

    ciclo["usado"] = D("12000")
    ciclo["abonos"] = [_acum("150.6", VENCE_1)]
    tid = uuid.uuid4()
    async with tenant_scope(tid):
        assert (await alcanza(_Sesion(), tid, 150)).limite == D("12150.6")
        with pytest.raises(QuotaExceeded) as exc:
            await alcanza(_Sesion(), tid, 151)
    assert (exc.value.used, exc.value.limit) == (12_000, 12_150)  # el saldo se informa hacia abajo
