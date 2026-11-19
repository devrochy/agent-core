"""Mínimo del pago en línea que ve el panel (HU-SUB-25).

El panel avisa al guardar un plan cuyo precio por período queda bajo el mínimo de la pasarela, y
ese mínimo cambia con el ambiente (Wompi: 150.000 COP en sandbox, 1.500 en producción) y con la
política del negocio (`pago_en_linea_monto_minimo`). Por eso se pregunta al servidor y el panel no
lleva una cifra. Es la misma regla que aplica `crear_pedido` (`OrderSkill._minimo_pago_en_linea`),
con una diferencia a propósito: sin pasarela que declare un mínimo no hay valor (`None`) y por tanto
no hay aviso, mientras que `crear_pedido` conserva su política anterior de 150.000.
"""

from __future__ import annotations

from typing import Any, Literal

from core.shared.tenant import get_current_tenant_id

FuenteMinimo = Literal["negocio", "pasarela", "ninguna"]


async def _capacidad_de_pago() -> Any:
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments import payment_registry

    spec = await load_active_spec(get_current_tenant_id())
    return payment_registry.capability_for(enabled_packs(spec))


async def minimo_efectivo(config: Any) -> tuple[float | None, FuenteMinimo]:
    """`(minimo, fuente)` del negocio activo. El valor explícito del negocio manda sobre el de la
    pasarela; sin ninguno de los dos, `(None, "ninguna")`."""
    explicito = getattr(config, "pago_en_linea_monto_minimo", None)
    if explicito is not None:
        return float(explicito), "negocio"
    from core.payments import monto_minimo

    declarado = await monto_minimo(await _capacidad_de_pago())
    if declarado is None:
        return None, "ninguna"
    return declarado, "pasarela"


async def cuentas_de_transferencia_configuradas() -> int:
    """Cuántas cuentas de transferencia tiene el negocio (0 si no hay proveedor ni cuentas)."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments.transferencia import transfer_registry

    spec = await load_active_spec(get_current_tenant_id())
    proveedor = transfer_registry.provider_for(enabled_packs(spec))
    if proveedor is None:
        return 0
    try:
        return len(await proveedor.cuentas_de_transferencia())
    except Exception:
        from core.shared.logger import get_logger

        get_logger(__name__).warning("minimo_cuentas_fallo", exc_info=True)
        return 0
