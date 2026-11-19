"""Cuentas de transferencia de prueba para los tests de avisos de suscripciones (HU-SUB-24).

Un cargo sin link solo se avisa si el negocio tiene cuentas de transferencia. Los negocios de estos
tests solo activan `pack_subscriptions` (sin pasarela), así que un proveedor falso hace de comercio.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.payments.transferencia import transfer_registry

CUENTA = {"entity": "Nequi", "accountNumber": "3001112233", "holderName": "Negocio de prueba"}


class CuentasFalsas:
    plugin_id = "pack_subscriptions"

    def __init__(self, cuentas: list[dict[str, Any]]) -> None:
        self._cuentas = cuentas

    async def cuentas_de_transferencia(self) -> list[dict[str, Any]]:
        return self._cuentas


@pytest.fixture
def con_cuentas_de_transferencia():
    transfer_registry.register("pack_subscriptions", CuentasFalsas([CUENTA]))
    yield CUENTA
    transfer_registry.unregister("pack_subscriptions")
