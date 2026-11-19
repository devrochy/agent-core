"""El mínimo del pago en línea lo declara la pasarela según su ambiente (HU-SUB-13, adenda de ADR-0067).

El 150.000 COP salió de un 422 del sandbox de Wompi; en producción el mínimo publicado es 1.500. El
gate de `crear_pedido` ya no fija una cifra propia: pregunta a la capacidad de pagos del core, y un
valor explícito del negocio manda sobre el de la pasarela.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pack_commerce.config import CRMConfig
from pack_commerce.skills import OrderSkill
from pack_payments import payments as capacidad

from core.payments import monto_minimo


class _Capacidad:
    plugin_id = "falsa"

    def __init__(self, minimo: float | None) -> None:
        self._minimo = minimo

    async def monto_minimo(self) -> float | None:
        return self._minimo


class _SinMinimo:
    plugin_id = "sin_minimo"


def _skill(capacidad_de_pago: object | None) -> OrderSkill:
    skill = OrderSkill.__new__(OrderSkill)

    async def _cap() -> object | None:
        return capacidad_de_pago

    skill._payment_capability = _cap  # type: ignore[method-assign]
    return skill


@pytest.mark.asyncio
async def test_core_devuelve_el_minimo_que_declara_la_capacidad() -> None:
    assert await monto_minimo(_Capacidad(1_500)) == 1_500.0


@pytest.mark.asyncio
async def test_core_devuelve_none_sin_capacidad_o_sin_el_metodo() -> None:
    assert await monto_minimo(None) is None
    assert await monto_minimo(_SinMinimo()) is None
    assert await monto_minimo(_Capacidad(None)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("sandbox", "esperado"), [(True, 150_000.0), (False, 1_500.0)])
async def test_wompi_declara_el_minimo_de_su_ambiente(monkeypatch, sandbox: bool, esperado: float) -> None:
    async def _config() -> SimpleNamespace:
        return SimpleNamespace(sandbox=sandbox)

    monkeypatch.setattr(capacidad, "tenant_config", _config)
    assert await capacidad.WompiPaymentCapability().monto_minimo() == esperado


@pytest.mark.asyncio
async def test_wompi_sin_config_no_declara_minimo(monkeypatch) -> None:
    async def _config() -> SimpleNamespace:
        raise RuntimeError("sin config")

    monkeypatch.setattr(capacidad, "tenant_config", _config)
    assert await capacidad.WompiPaymentCapability().monto_minimo() is None


@pytest.mark.asyncio
async def test_comercio_usa_el_minimo_de_la_pasarela_si_el_negocio_no_fijo_uno() -> None:
    config = SimpleNamespace(pago_en_linea_monto_minimo=None)
    assert await _skill(_Capacidad(1_500))._minimo_pago_en_linea(config) == 1_500.0
    assert await _skill(_Capacidad(150_000))._minimo_pago_en_linea(config) == 150_000.0


@pytest.mark.asyncio
async def test_un_valor_explicito_del_negocio_manda_sobre_la_pasarela() -> None:
    assert await _skill(_Capacidad(1_500))._minimo_pago_en_linea(SimpleNamespace(pago_en_linea_monto_minimo=50_000)) == 50_000.0
    # 0 apaga el gate (antes `or 150_000` lo volvía a prender).
    assert await _skill(_Capacidad(150_000))._minimo_pago_en_linea(SimpleNamespace(pago_en_linea_monto_minimo=0)) == 0.0


@pytest.mark.asyncio
async def test_sin_pasarela_que_lo_declare_queda_el_de_sandbox() -> None:
    config = SimpleNamespace(pago_en_linea_monto_minimo=None)
    assert await _skill(None)._minimo_pago_en_linea(config) == 150_000.0
    assert await _skill(_SinMinimo())._minimo_pago_en_linea(config) == 150_000.0


def test_la_config_de_comercio_no_trae_una_cifra_por_defecto() -> None:
    assert CRMConfig().pago_en_linea_monto_minimo is None
    assert CRMConfig(pago_en_linea_monto_minimo=1_500).pago_en_linea_monto_minimo == 1_500
