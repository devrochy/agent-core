"""Procedimiento `pack_subscriptions.cambiar_plan` y su *resolver* (HU-AOS-08, ADR-0113 §1).

El JSON vive en `packages/pack-subscriptions/procedures/`. Las acciones son del core (ADR-0106:
un solo juego de tools `sub_*`): `sub_consultar`, `sub_cotizar_cambio_plan` y
`sub_aplicar_cambio_plan` (adenda de ADR-0111). El precio y el prorrateo salen del catálogo del
servidor (ADR-0111) y el monto que el cliente confirma con el botón se compara en el servidor con
el que se cobra.

`plan_destino` valida el plan que escribe el modelo con la misma cotización que la tool: el plan
existe en el catálogo del negocio y el cambio es posible (no es el mismo plan, la suscripción está
activa, misma periodicidad…). Si no, devuelve el rechazo del dominio en lenguaje claro.
"""

from __future__ import annotations

from typing import Any

from core.mcp.types import ResolverResult, ToolContext
from core.subscriptions import SubscriptionError


async def plan_destino(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    from core.subscriptions.tools import cotizacion_del_cliente

    try:
        cotizacion = await cotizacion_del_cliente(ctx, str(valor or ""))
    except SubscriptionError as exc:
        return ResolverResult(ok=False, error=exc.message)
    return ResolverResult(ok=True, value=cotizacion["plan_code"])


def registrar(host: Any, plugin_id: str) -> None:
    """Publica el procedimiento y su resolver (lo llama `on_activate`). Sin banderas encendidas
    nadie los usa: el motor solo actúa con `tenants.aos_procedures` (o en sombra con `aos_runtime = v3`)."""
    from core.kernel.procedures import de_pack

    host.register_resolver(f"{plugin_id}.plan_destino", validar=plan_destino)
    host.register_procedures(de_pack.cargar(plugin_id))
