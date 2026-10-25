"""Datos de transferencia del negocio como capacidad del core (HU-SUB-24, adenda de ADR-0067).

Comercio registra sus cuentas; suscripciones las consulta por el core sin importarlo (invariante
nº5) y arma el aviso del cargo sin link con ellas. El bloque lo escribe el código, nunca el modelo.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pack_commerce import skills as comercio
from pack_commerce.transferencia import CuentasDeTransferenciaDelNegocio
from pack_subscriptions.config import SubscriptionConfig
from pack_subscriptions.jobs import _payload_cargo, payload_cargo_nuevo

import core.gateway.event_notices as notices
from core.payments.transferencia import (
    TransferDetailsRegistry,
    datos_de_transferencia,
    formatear_cuentas,
    transfer_registry,
)

BANCOLOMBIA = {
    "entity": "Bancolombia",
    "accountType": "Ahorros",
    "accountNumber": "123-456",
    "holderName": "Ana Pérez",
    "holderDocument": "1001",
}
NEQUI = {"entity": "Nequi", "accountNumber": "3001112233"}


class _Proveedor:
    def __init__(self, plugin_id: str, cuentas: list[dict[str, Any]] | Exception) -> None:
        self.plugin_id = plugin_id
        self._cuentas = cuentas

    async def cuentas_de_transferencia(self) -> list[dict[str, Any]]:
        if isinstance(self._cuentas, Exception):
            raise self._cuentas
        return self._cuentas


def test_el_bloque_lleva_cada_cuenta_con_sus_datos() -> None:
    assert formatear_cuentas([BANCOLOMBIA, NEQUI]) == (
        "💳 Datos para tu transferencia:\n"
        "• Bancolombia (Ahorros) N° 123-456 a nombre de Ana Pérez CC 1001\n"
        "• Nequi N° 3001112233"
    )


@pytest.mark.parametrize("vacio", [None, []])
def test_sin_cuentas_no_hay_bloque(vacio) -> None:
    assert formatear_cuentas(vacio) == ""


def test_el_registro_filtra_por_pack_habilitado() -> None:
    registro = TransferDetailsRegistry()
    comercio_ = _Proveedor("pack_commerce", [NEQUI])
    registro.register("pack_commerce", comercio_)
    assert registro.provider_for({"pack_commerce"}) is comercio_
    assert registro.provider_for({"pack_subscriptions"}) is None
    assert registro.provider_for(None) is comercio_
    registro.unregister("pack_commerce")
    assert registro.provider_for(None) is None


@pytest.mark.asyncio
async def test_datos_de_transferencia_usa_el_proveedor_del_pack_habilitado(monkeypatch) -> None:
    registro = TransferDetailsRegistry()
    registro.register("pack_commerce", _Proveedor("pack_commerce", [NEQUI]))
    monkeypatch.setattr("core.payments.transferencia.transfer_registry", registro)
    assert "Nequi N° 3001112233" in await datos_de_transferencia({"pack_commerce", "pack_subscriptions"})
    assert await datos_de_transferencia({"pack_subscriptions"}) == ""


@pytest.mark.asyncio
async def test_un_proveedor_que_falla_cuenta_como_sin_datos(monkeypatch) -> None:
    registro = TransferDetailsRegistry()
    registro.register("pack_commerce", _Proveedor("pack_commerce", RuntimeError("config rota")))
    monkeypatch.setattr("core.payments.transferencia.transfer_registry", registro)
    assert await datos_de_transferencia(None) == ""


@pytest.mark.asyncio
async def test_comercio_ofrece_sus_cuentas_y_el_chat_imprime_el_mismo_bloque(monkeypatch) -> None:
    config = SimpleNamespace(transfer_accounts=[BANCOLOMBIA, NEQUI])

    async def _config() -> SimpleNamespace:
        return config

    monkeypatch.setattr(comercio, "_plugin_config", _config)
    assert await CuentasDeTransferenciaDelNegocio().cuentas_de_transferencia() == [BANCOLOMBIA, NEQUI]
    # Comercio y suscripciones imprimen lo mismo: el chat de ventas no cambió con la capacidad.
    assert await comercio._transfer_safe_append() == formatear_cuentas([BANCOLOMBIA, NEQUI])


@pytest.mark.asyncio
async def test_comercio_sin_config_o_sin_cuentas_no_ofrece_nada(monkeypatch) -> None:
    async def _sin_config() -> None:
        return None

    monkeypatch.setattr(comercio, "_plugin_config", _sin_config)
    assert await CuentasDeTransferenciaDelNegocio().cuentas_de_transferencia() == []
    assert await comercio._transfer_safe_append() == ""


def test_comercio_queda_registrado_en_el_core_al_activarse() -> None:
    from unittest.mock import MagicMock

    from pack_commerce.module import PLUGIN_ID, CRMModule

    transfer_registry.unregister(PLUGIN_ID)
    CRMModule.__new__(CRMModule).on_activate(MagicMock())
    assert isinstance(transfer_registry.provider_for({PLUGIN_ID}), CuentasDeTransferenciaDelNegocio)


# ------------------------------------------------------------------ aviso del cargo
def _cargo(link: str | None) -> tuple[Any, Any]:
    import uuid
    from datetime import UTC, datetime
    from decimal import Decimal

    cargo = SimpleNamespace(
        id=uuid.uuid4(), periodo="2026-11", monto=Decimal("80000"), referencia=None, link_url=link,
        fecha_vencimiento=datetime(2026, 11, 15, tzinfo=UTC),
    )
    sub = SimpleNamespace(id=uuid.uuid4(), customer_id=uuid.uuid4(), concepto="Plan Pro", moneda="COP")
    return cargo, sub


def test_el_payload_sin_link_lleva_los_datos_de_transferencia() -> None:
    cargo, sub = _cargo(None)
    bloque = formatear_cuentas([NEQUI])
    payload = _payload_cargo(cargo, sub, bloque)
    assert payload["link"] is None
    assert payload["datos_transferencia"] == bloque
    assert payload["forma_de_pago"] == bloque


def test_el_payload_con_link_no_cambia_de_forma() -> None:
    cargo, sub = _cargo("https://pago.test/abc")
    payload = payload_cargo_nuevo(cargo, sub, "texto")
    assert payload["link"] == "https://pago.test/abc"
    assert payload["datos_transferencia"] is None
    assert payload["forma_de_pago"] == "https://pago.test/abc"
    assert payload["texto"] == "texto"


@pytest.mark.parametrize(
    "texto",
    [
        SubscriptionConfig().aviso_cargo_transferencia_texto,
        SubscriptionConfig().recordatorio_transferencia_texto,
        SubscriptionConfig().aviso_pausa_transferencia_texto,
    ],
)
def test_los_textos_de_transferencia_llevan_las_cuentas_y_ningun_link(texto: str) -> None:
    assert "{cargo.datos_transferencia}" in texto
    assert "{cargo.link}" not in texto


def test_el_texto_de_respaldo_de_transferencia_se_arma_con_el_contexto_del_servidor() -> None:
    contexto = {
        "cliente": {"nombre": "Ana"},
        "suscripcion": {"concepto": "Plan Pro"},
        "cargo": {
            "monto": "$80.000", "fecha_vencimiento": "2026-11-15", "periodo": "2026-11", "link": None,
            "datos_transferencia": formatear_cuentas([NEQUI]),
        },
    }
    texto = notices.render_fallback(
        SubscriptionConfig().aviso_cargo_transferencia_texto, contexto, "subscription.charge_created"
    )
    assert "Nequi N° 3001112233" in texto
    assert "Plan Pro 2026-11" in texto
    assert "{" not in texto  # ningún marcador quedó sin resolver


@pytest.mark.parametrize("evento", ["subscription.charge_created", "subscription.charge_reminder", "subscription.paused"])
def test_las_plantillas_pueden_mapear_la_forma_de_pago(evento: str) -> None:
    for ruta in ("cargo.datos_transferencia", "cargo.forma_de_pago"):
        assert ruta in notices.EVENTOS[evento]


def test_una_plantilla_con_forma_de_pago_se_resuelve_sin_link() -> None:
    """La plantilla que mapea `cargo.link` falla sin link; `cargo.forma_de_pago` cubre ambos casos."""
    bloque = formatear_cuentas([NEQUI])
    contexto = {"cargo": {"link": None, "forma_de_pago": bloque}}
    valores = notices.resolve_values("subscription.charge_created", ["cargo.forma_de_pago"], ["1"], contexto)
    assert "Nequi" in valores["1"]
    assert "\n" not in valores["1"]  # Meta rechaza saltos de línea en un parámetro
    with pytest.raises(notices.NoticeConfigError):
        notices.resolve_values("subscription.charge_created", ["cargo.link"], ["1"], contexto)
