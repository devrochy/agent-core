"""HU-BILL-04/06 · crédito mensual: tarifario, créditos del turno y mes de consumo (ADR-0144).

Puro, sin base ni LLM. La medición contra la base está en
`tests/integration/test_credito_mensual_medicion.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.billing.periodos import ventana_mensual
from core.billing.tarifario import (
    TARIFARIO_V1,
    creditos_por_evento,
    creditos_variables,
    trm_vigente,
)


def test_formula_del_modelo_de_precios() -> None:
    # modelo-de-precios §3: conversación de 8 turnos con deepseek-flash, USD 0,0069 → 8,3 + fees.
    assert creditos_variables(Decimal("0.0069"), Decimal(4000)) == Decimal("8.3")  # ⌈82,8⌉/10
    assert creditos_por_evento(0.0069, "llm_turn", "platform", Decimal(4000)) == Decimal("8.5")
    # Imagen con claude-haiku (USD 0,0035): 4,2 + 1,0 = 5,2 (tabla §3).
    assert creditos_por_evento(0.0035, "vision", "platform", Decimal(4000)) == Decimal("5.2")


def test_redondeo_hacia_arriba_a_la_decima() -> None:
    # 0,000001 USD × 4000 × 3 / 10 = 0,0012 → 0,1 (nunca se regala la fracción).
    assert creditos_variables(Decimal("0.000001"), Decimal(4000)) == Decimal("0.1")
    assert creditos_variables(Decimal(0), Decimal(4000)) == Decimal("0.0")
    assert creditos_variables(Decimal("-1"), Decimal(4000)) == Decimal("0.0")


def test_llave_propia_solo_paga_el_fee() -> None:
    for concepto, fee in TARIFARIO_V1.fees.items():
        assert creditos_por_evento(0.5, concepto, "byok", Decimal(4000)) == fee  # type: ignore[arg-type]


def test_la_tasa_cambia_los_creditos_nuevos() -> None:
    assert creditos_variables(Decimal("0.0069"), Decimal(4400)) == Decimal("9.2")  # ⌈91,08⌉/10


def test_trm_por_defecto_y_por_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from core.config import Settings, get_settings

    monkeypatch.delenv("BILLING_TRM_COP", raising=False)
    assert Settings().billing_trm_cop == 4000  # supuesto del modelo de precios, pendiente del dueño
    monkeypatch.setenv("BILLING_TRM_COP", "4250.5")
    assert Settings().billing_trm_cop == 4250.5
    monkeypatch.setenv("BILLING_TRM_COP", "40")  # fuera de rango: el setting no carga
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.delenv("BILLING_TRM_COP")
    monkeypatch.setattr(get_settings(), "billing_trm_cop", 4250.5)
    assert trm_vigente() == Decimal("4250.5")


def test_creditos_del_turno_al_escribir() -> None:
    from core.orchestrator.loop import _creditos_del_turno, _TurnUsage

    usage = _TurnUsage(cost_usd=0.0069, llamadas=1)
    assert _creditos_del_turno(usage) == {
        "credits": Decimal("8.5"), "key_mode": "platform", "tarifario_version": "v1", "trm_cop": Decimal("4000.0"),
    }
    usage.key_mode = "byok"
    assert _creditos_del_turno(usage)["credits"] == Decimal("0.2")
    # Sin llamada al LLM (presupuesto agotado antes de la primera): sin fee de turno.
    assert _creditos_del_turno(_TurnUsage())["credits"] == Decimal("0.0")


def test_si_el_calculo_falla_la_fila_se_escribe_sin_creditos(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.billing import tarifario
    from core.orchestrator.loop import _creditos_del_turno, _TurnUsage

    def _rompe() -> Decimal:
        raise RuntimeError("tarifario roto")

    monkeypatch.setattr(tarifario, "trm_vigente", _rompe)
    assert _creditos_del_turno(_TurnUsage(cost_usd=0.01, llamadas=1)) == {}


def test_llave_propia_misma_condicion_que_el_proveedor() -> None:
    from types import SimpleNamespace

    from core.orchestrator.loop import _usa_llave_propia

    assert _usa_llave_propia(None) is False
    assert _usa_llave_propia(SimpleNamespace(llm_provider="openai", llm_api_key_encrypted=None)) is False  # type: ignore[arg-type]
    assert _usa_llave_propia(SimpleNamespace(llm_provider=None, llm_api_key_encrypted="x")) is False  # type: ignore[arg-type]
    assert _usa_llave_propia(SimpleNamespace(llm_provider="openai", llm_api_key_encrypted="x")) is True  # type: ignore[arg-type]


def _d(anio: int, mes: int, dia: int) -> datetime:
    return datetime(anio, mes, dia, 12, 0, tzinfo=UTC)


def test_el_mes_de_consumo_arranca_en_el_ciclo() -> None:
    assert ventana_mensual(_d(2026, 10, 5), 5, _d(2026, 10, 19)) == (_d(2026, 10, 5), _d(2026, 11, 5))


def test_el_credito_se_reinicia_cada_mes_aunque_period_start_no_se_mueva() -> None:
    # Renovación anticipada: `period_start` se queda en septiembre y `period_end` se alarga.
    assert ventana_mensual(_d(2026, 9, 5), 5, _d(2026, 10, 19)) == (_d(2026, 10, 5), _d(2026, 11, 5))
    # Justo en el corte, el mes nuevo empieza en cero.
    assert ventana_mensual(_d(2026, 9, 5), 5, _d(2026, 10, 5))[0] == _d(2026, 10, 5)


def test_dia_ancla_31_en_meses_cortos_y_sin_ancla() -> None:
    inicio, fin = ventana_mensual(_d(2026, 1, 31), 31, _d(2026, 3, 10))
    assert (inicio.date().isoformat(), fin.date().isoformat()) == ("2026-02-28", "2026-03-31")
    # Prueba gratis sin día ancla: el ancla es el día de `period_start`.
    assert ventana_mensual(_d(2026, 10, 18), None, _d(2026, 10, 19)) == (_d(2026, 10, 18), _d(2026, 11, 18))


def test_antes_de_empezar_devuelve_el_primer_tramo() -> None:
    assert ventana_mensual(_d(2026, 11, 1), 1, _d(2026, 10, 19)) == (_d(2026, 11, 1), _d(2026, 12, 1))


async def test_si_la_fila_con_creditos_falla_se_escribe_sin_ellos(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from core.orchestrator import loop

    escritas: list = []
    intentos = {"n": 0}

    class _Sesion:
        def add(self, fila) -> None:  # type: ignore[no-untyped-def]
            escritas.append(fila)

    @asynccontextmanager
    async def _sesion():  # type: ignore[no-untyped-def]
        intentos["n"] += 1
        yield _Sesion()
        if intentos["n"] == 1:
            raise RuntimeError("column credits does not exist")  # 0096 sin aplicar

    monkeypatch.setattr(loop, "get_session", _sesion)
    spec = SimpleNamespace(version="1.0.0", model=SimpleNamespace(primary="deepseek-flash"))
    await loop._write_usage_ledger(uuid.uuid4(), spec, loop._TurnUsage(cost_usd=0.01, llamadas=1))
    assert intentos["n"] == 2
    assert escritas[0].credits == Decimal("12.2") and escritas[1].credits is None
    assert float(escritas[1].cost_usd) == 0.01  # la fila de facturación no se pierde
