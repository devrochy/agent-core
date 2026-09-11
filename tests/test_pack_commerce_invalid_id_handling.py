"""IDs inválidos del LLM no deben tirar una excepción no manejada.

Reproduce el incidente real (tenant `yopoldo`, 2026-09-11): el LLM llamó
`generar_cotizacion` con `variant_id="pollo_broaster"` en vez del UUID real
que había devuelto `consultar_inventario`. El string inválido llegaba
directo a Postgres (`asyncpg.exceptions.DataError`), una excepción no
manejada que el core interpreta como una falla real del plugin y lo
desactiva -- GLOBALMENTE, para TODOS los tenants (`PluginRecord.status` no
es por-tenant). Confirmado en logs de producción de test: `plugin_commerce`
y `pack_payments` quedaron en estado `error` para todo el proceso.
"""

from __future__ import annotations

import uuid

import pytest
from pack_commerce.skills import _get_variant_with_stock, _parse_uuid


def test_parse_uuid_valid():
    parsed = _parse_uuid(str(uuid.uuid4()))
    assert isinstance(parsed, uuid.UUID)


@pytest.mark.parametrize("value", ["pollo_broaster", "", "123", None, 42])
def test_parse_uuid_invalid_returns_none(value):
    assert _parse_uuid(value) is None


@pytest.mark.asyncio
async def test_get_variant_with_stock_invalid_id_returns_none_not_exception():
    """El caso real exacto: antes esto llegaba a Postgres y tiraba DataError."""
    result = await _get_variant_with_stock("pollo_broaster")
    assert result is None
