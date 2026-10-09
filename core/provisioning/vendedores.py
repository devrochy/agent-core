"""Allowlist de negocios vendedores (HU-SH-03, ADR-0120 § 2).

`PROVISIONING_SELLER_TENANTS`: slugs o UUID separados por coma. Vacía = el alta automática
está apagada (fail-closed). Es una variable de entorno y no una opción del panel: ningún
admin de un negocio puede darse permiso de crear negocios.
"""

from __future__ import annotations

import uuid

from core.config import get_settings
from core.db.models import Tenant
from core.db.session import get_session


def vendedores_configurados() -> frozenset[str]:
    """Entradas de la allowlist normalizadas (minúsculas, sin espacios ni vacías)."""
    raw = get_settings().provisioning_seller_tenants or ""
    return frozenset(parte.strip().lower() for parte in raw.split(",") if parte.strip())


async def es_vendedor(tenant_id: uuid.UUID) -> bool:
    """¿El negocio puede vender negocios? Su id o su slug están en la allowlist y está
    activo y sin borrar. `tenants` es tabla de sistema: se lee sin depender de la RLS."""
    permitidos = vendedores_configurados()
    if not permitidos:
        return False
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.deleted_at is not None or tenant.status != "active":
        return False
    return str(tenant.id).lower() in permitidos or (tenant.slug or "").lower() in permitidos


__all__ = ["es_vendedor", "vendedores_configurados"]
