"""Líneas de crédito de facturación -- SOLO LECTURA. Meta no soporta
create/update/delete sobre `extendedcredits` (`POST /{business_id}/
extendedcredits` NO EXISTE); el alta del método de pago del cliente no
tiene API y se resuelve con un deep-link manual a WhatsApp Manager /
Business Settings (ver `core/admin/views.py`).

NO se implementa `owning_credit_allocation_configs`: exige ser Solution
Partner y aceptar términos manualmente en Business Settings, y su
documentación indexada es de v14.0 (poco mantenida) -- fuera de alcance
por decisión explícita, no un TODO pendiente.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.gateway.meta_graph import graph_fetch


async def get_extended_credits(
    business_id: str, token: str, app_secret: str, version: str, connection_id: uuid.UUID
) -> list[dict[str, Any]]:
    """`GET /{business_id}/extendedcredits` -- devuelve la lista cruda tal
    cual la manda Meta. No se le atribuye ningún significado de "método de
    pago configurado: sí/no": Meta no da esa semántica directamente."""
    data = await graph_fetch(
        f"/{business_id}/extendedcredits", token=token, app_secret=app_secret,
        version=version, connection_id=connection_id,
    )
    return (data or {}).get("data") or []
