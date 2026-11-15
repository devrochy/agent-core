"""Simuladores de Comercio en la prueba del panel: el stock (HU-ARCH-14).

`_get_variant_with_stock` devuelve `variant`, `product`, `stock` y `price`. El simulador leía
además `sin_inventario`, que esa función nunca devolvió: cada cotización de la prueba lanzaba
`KeyError` y ADR-0024 apagaba `pack_commerce` para el negocio («el agente no puede acceder al
inventario»). El simulador valida el stock igual que el handler real.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pack_commerce import sandbox

from core.mcp.types import ToolContext


def _variante(stock: int, precio: float = 10.0) -> dict[str, Any]:
    return {
        "variant": SimpleNamespace(id=uuid.uuid4(), sku="A-001", active=True, attributes={}),
        "product": SimpleNamespace(name="Aguardiente"),
        "stock": stock,
        "price": precio,
    }


class _SandboxEnMemoria:
    def __init__(self) -> None:
        self.datos: dict[tuple[str, str], dict[str, Any]] = {}

    async def leer(self, espacio: str, clave: str) -> dict[str, Any] | None:
        return self.datos.get((espacio, clave))

    async def guardar(self, espacio: str, clave: str, valor: dict[str, Any]) -> None:
        self.datos[(espacio, clave)] = valor


def _con_variante(monkeypatch: pytest.MonkeyPatch, data: dict[str, Any] | None) -> None:
    async def falsa(_ref: object) -> dict[str, Any] | None:
        return data

    monkeypatch.setattr("pack_commerce.skills._get_variant_with_stock", falsa)


@pytest.mark.asyncio
async def test_cotizacion_simulada_con_stock_suficiente(monkeypatch: pytest.MonkeyPatch) -> None:
    _con_variante(monkeypatch, _variante(stock=5))
    resultado = await sandbox._calcular_cotizacion({"items": [{"variant_id": "A-001", "quantity": 2}]})
    assert isinstance(resultado, tuple)
    calculo, filas = resultado
    assert filas[0]["line_total"] == 20.0
    assert calculo


@pytest.mark.asyncio
async def test_cotizacion_simulada_sin_stock_responde_error_y_no_lanza(monkeypatch: pytest.MonkeyPatch) -> None:
    _con_variante(monkeypatch, _variante(stock=1))
    resultado = await sandbox._calcular_cotizacion({"items": [{"variant_id": "A-001", "quantity": 3}]})
    assert resultado == {"error": "No hay stock suficiente de Aguardiente (disponible: 1)."}


@pytest.mark.asyncio
async def test_agregar_item_simulado_valida_stock_sin_lanzar(monkeypatch: pytest.MonkeyPatch) -> None:
    caja = _SandboxEnMemoria()
    await caja.guardar("orders", "o-1", {"order_id": "o-1", "total": 10.0})
    contexto = ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key="k",
        channel="test",
        sandbox=caja,  # type: ignore[arg-type]
    )

    _con_variante(monkeypatch, _variante(stock=0))
    sin_stock = await sandbox.simular_agregar_item_pedido(
        {"order_id": "o-1", "items": [{"variant_id": "A-001", "quantity": 1}]}, contexto
    )
    assert sin_stock == {"error": "No hay stock suficiente de Aguardiente."}

    _con_variante(monkeypatch, _variante(stock=4, precio=5.0))
    agregado = await sandbox.simular_agregar_item_pedido(
        {"order_id": "o-1", "items": [{"variant_id": "A-001", "quantity": 2}]}, contexto
    )
    assert agregado["status"] == "updated" and agregado["nuevo_total"] == 20.0
