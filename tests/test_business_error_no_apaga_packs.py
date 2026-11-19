"""HU-ARCH-18: un error de negocio nunca apaga una habilidad.

- `BusinessError` (y sus subclases) se convierte en resultado controlado; el
  pack SIGUE habilitado.
- Una falla real (excepción que no es `BusinessError`) sigue disparando el
  auto-deshabilitado (ADR-0024).
- `PaymentCapabilityError` es una subclase de `BusinessError`.
"""

from __future__ import annotations

import pytest

from core.mcp.host import MCPHost
from core.mcp.types import MCPTool
from core.shared.exceptions import (
    BusinessError,
    PaymentCapabilityError,
    PluginError,
    SubscriptionError,
    ValidationError,
)


def _host_with(handler) -> MCPHost:
    host = MCPHost()
    tool = MCPTool(
        name="t",
        description="test",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    # Registrar directo (sin el ciclo de plugins).
    host._tools[tool.name] = tool
    return host


def _ctx():
    import uuid

    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key="test",
        channel="test",
    )


async def test_business_error_es_resultado_controlado() -> None:
    async def handler(_args, _ctx):
        raise PaymentCapabilityError("monto bajo el mínimo")

    host = _host_with(handler)
    result = await host.execute_tool("t", {}, _ctx())
    assert result == {"error": "monto bajo el mínimo"}


async def test_business_error_con_status_y_escalate() -> None:
    async def handler(_args, _ctx):
        raise BusinessError("no encontrado", code="cliente_no_encontrado", escalate=True)

    host = _host_with(handler)
    result = await host.execute_tool("t", {}, _ctx())
    assert result["status"] == "cliente_no_encontrado"
    assert result["escalate"] is True


async def test_business_error_no_se_convierte_en_plugin_error() -> None:
    async def handler(_args, _ctx):
        raise ValidationError("dato inválido")

    host = _host_with(handler)
    # No debe lanzar PluginError (que dispararía el auto-disable).
    result = await host.execute_tool("t", {}, _ctx())
    assert result["error"] == "dato inválido"


async def test_falla_real_si_lanza_plugin_error() -> None:
    async def handler(_args, _ctx):
        raise RuntimeError("base caída")

    host = _host_with(handler)
    with pytest.raises(PluginError):
        await host.execute_tool("t", {}, _ctx())


def test_jerarquia_de_excepciones() -> None:
    assert issubclass(PaymentCapabilityError, BusinessError)
    assert issubclass(SubscriptionError, BusinessError)
    assert issubclass(ValidationError, BusinessError)
    assert not issubclass(PluginError, BusinessError)


def test_plantilla_invalida_cae_al_texto_por_defecto() -> None:
    """Una plantilla de mensaje con una variable inexistente no rompe la tool."""
    from pack_commerce.skills import _safe_format

    # `{variable_inexistente}` no está en kwargs -> KeyError capturado.
    resultado = _safe_format("Hola {variable_inexistente}", "Hola por defecto", otro="x")
    assert resultado == "Hola por defecto"


def test_plantilla_valida_se_usa() -> None:
    from pack_commerce.skills import _safe_format

    assert _safe_format("Pedido {n}", "def", n="OC-1") == "Pedido OC-1"
