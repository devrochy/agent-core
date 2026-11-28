"""HU-FB-02 · el abono `bonificacion` en el saldo del ciclo (ADR-0213 §1-2), sin base.

- `repartir` gasta mes → acumulado (el que vence antes) → bonificaciones → recargas: lo regalado se gasta
  antes que lo pagado;
- el reparto conserva la suma: lo usado de cada fuente más el colchón es siempre lo consumido, y `total`
  suma las bonificaciones;
- `_saldo_del_mes` suma las bonificaciones al límite;
- `acreditar_bonificacion` rechaza montos no enteros o no positivos y exige el `tenant_scope`.

La integración contra Postgres (CHECKs, arrastre al cierre, idempotencia del abono y la migración 0161) está
en `tests/integration/test_credit_grants_bonificacion.py`.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.billing.creditos import TIPOS_DE_ABONO, Abono, orden_de_gasto, repartir

D = Decimal
VENCE_1 = datetime(2026, 12, 10, tzinfo=UTC)
VENCE_2 = datetime(2027, 1, 10, tzinfo=UTC)


def _acum(creditos: str, vence: datetime = VENCE_1, origen: str = "a") -> Abono:
    return Abono("acumulado", D(creditos), origen, vence)


def _bono(creditos: str, origen: str = "feedback:b") -> Abono:
    return Abono("bonificacion", D(creditos), origen)


def _recarga(creditos: str, origen: str = "r") -> Abono:
    return Abono("recarga", D(creditos), origen)


def test_bonificacion_es_un_tipo_de_abono() -> None:
    assert TIPOS_DE_ABONO == ("acumulado", "recarga", "bonificacion")


# ------------------------------------------------------------- orden de gasto con los tres tipos
def test_el_orden_es_acumulado_luego_bonificacion_y_al_final_recarga() -> None:
    abonos = [_recarga("1", "r1"), _bono("1", "feedback:2"), _acum("1", VENCE_2, "a2"), _bono("1", "feedback:1"),
              _acum("1", VENCE_1, "a1"), _recarga("1", "r0")]
    assert [a.origen for a in orden_de_gasto(abonos)] == [
        "a1", "a2", "feedback:1", "feedback:2", "r0", "r1",
    ]
    # Reproducible con cualquier orden de lectura de la base.
    assert orden_de_gasto(list(reversed(abonos))) == orden_de_gasto(abonos)


@pytest.mark.parametrize(
    ("usado", "mes", "acumulado", "bonos", "recargas", "colchon"),
    [
        ("0", "0", "0", "0", "0", "0"),
        ("12000", "12000", "0", "0", "0", "0"),
        ("13500", "12000", "1500", "0", "0", "0"),
        ("14000", "12000", "2000", "0", "0", "0"),
        ("16000", "12000", "2000", "2000", "0", "0"),
        ("19000", "12000", "2000", "5000", "0", "0"),
        ("20000", "12000", "2000", "5000", "1000", "0"),
        ("22000", "12000", "2000", "5000", "3000", "0"),
        ("22100.5", "12000", "2000", "5000", "3000", "100.5"),
    ],
)
def test_se_gasta_mes_acumulado_bonificacion_y_recarga(usado, mes, acumulado, bonos, recargas, colchon) -> None:
    reparto = repartir(D(usado), 12_000, [_recarga("3000"), _bono("5000"), _acum("2000")])
    assert reparto.usado_mes == D(mes)
    assert reparto.usado_acumulado == D(acumulado)
    assert reparto.usado_bonificaciones == D(bonos)
    assert reparto.usado_recargas == D(recargas)
    assert reparto.sobre_el_saldo == D(colchon)
    assert (reparto.acumulado, reparto.bonificaciones, reparto.recargas) == (D(2000), D(5000), D(3000))
    assert reparto.total == D(22_000)


def test_con_solo_bonificacion_el_mes_se_gasta_primero() -> None:
    reparto = repartir(D(1_500), 1_500, [_bono("5000")])
    assert (reparto.usado_mes, reparto.usado_bonificaciones) == (D(1_500), D(0))
    assert reparto.total == D(6_500)


def test_el_reparto_conserva_la_suma() -> None:
    """Para cualquier consumo y cualquier mezcla de abonos: Σ usado por fuente + colchón = consumido, ninguna
    fuente da más de lo que tiene y `total` es la suma de las fuentes."""
    azar = random.Random(213)
    tipos = ("acumulado", "bonificacion", "recarga")
    for _ in range(500):
        abonos = []
        for i in range(azar.randint(0, 6)):
            tipo = azar.choice(tipos)
            creditos = D(azar.randint(1, 50_000)) / 10
            vence = VENCE_1 + timedelta(days=azar.randint(0, 90)) if tipo == "acumulado" else None
            abonos.append(Abono(tipo, creditos, f"o{i}", vence))
        mes = azar.choice([0, 1_500, 12_000, 40_000])
        usado = D(azar.randint(0, 120_000)) / 10
        r = repartir(usado, mes, abonos)
        usado_fuentes = r.usado_mes + r.usado_acumulado + r.usado_bonificaciones + r.usado_recargas
        assert usado_fuentes + r.sobre_el_saldo == usado
        assert r.usado_mes <= r.mes and all(u.usado <= u.abono.creditos for u in r.abonos)
        assert r.total == D(mes) + sum((a.creditos for a in abonos), D(0))
        # El colchón solo aparece cuando todo el saldo está gastado.
        assert r.sobre_el_saldo == 0 or usado_fuentes == r.total


# ------------------------------------------------------------------ `_saldo_del_mes` con bonificación
async def test_el_limite_del_turno_suma_las_bonificaciones(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.billing import creditos, enforcement
    from core.shared.tenant import tenant_scope

    overview = SimpleNamespace(plan_id="basic", period_start=datetime.now(UTC) - timedelta(days=3), dia_ancla=None)

    async def _sub(_tid):  # type: ignore[no-untyped-def]
        return overview

    async def _medir(_session, _tid, **_k):  # type: ignore[no-untyped-def]
        return creditos.ConsumoDeCreditos(total=D(14_000), por_concepto={})

    async def _abonos(_session, _tid, _ciclo):  # type: ignore[no-untyped-def]
        return [_recarga("1000"), _bono("3000"), _acum("500", datetime.now(UTC) + timedelta(days=60))]

    monkeypatch.setattr(enforcement, "subscription_overview", _sub)
    monkeypatch.setattr(creditos, "medir_creditos", _medir)
    monkeypatch.setattr(creditos, "leer_abonos", _abonos)
    tid = uuid.uuid4()
    async with tenant_scope(tid):
        saldo = await creditos._saldo_del_mes(SimpleNamespace(), tid, now=None, con_lock=False)
    assert saldo.limite == D(12_000 + 500 + 3_000 + 1_000)
    assert saldo.desglose is not None
    # 12.000 del mes, 500 de lo acumulado y 1.500 de la bonificación; la recarga queda intacta.
    assert (saldo.desglose.usado_bonificaciones, saldo.desglose.usado_recargas) == (D(1_500), D(0))
    assert saldo.disponible == D(2_500)


# ------------------------------------------------------------------ `acreditar_bonificacion`, validación
@pytest.mark.parametrize("creditos", [0, -5, 1.5, D("10"), True, "100"])
async def test_acreditar_rechaza_montos_que_no_son_enteros_positivos(creditos) -> None:  # type: ignore[no-untyped-def]
    from core.billing.bonificacion import acreditar_bonificacion

    with pytest.raises(ValueError, match="entero"):
        await acreditar_bonificacion(SimpleNamespace(), uuid.uuid4(), "feedback:x", creditos)


@pytest.mark.parametrize("origen", ["", "x" * 201])
async def test_acreditar_rechaza_un_origen_vacio_o_largo(origen) -> None:  # type: ignore[no-untyped-def]
    from core.billing.bonificacion import acreditar_bonificacion

    with pytest.raises(ValueError, match="origen"):
        await acreditar_bonificacion(SimpleNamespace(), uuid.uuid4(), origen, 100)


async def test_acreditar_exige_el_tenant_scope_del_negocio() -> None:
    from core.billing.bonificacion import acreditar_bonificacion
    from core.shared.tenant import tenant_scope

    async with tenant_scope(uuid.uuid4()):
        with pytest.raises(RuntimeError, match="tenant_scope"):
            await acreditar_bonificacion(SimpleNamespace(), uuid.uuid4(), "feedback:x", 100)
