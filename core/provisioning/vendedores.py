"""Allowlist de negocios vendedores (HU-SH-03, ADR-0121 § 2).

`PROVISIONING_SELLER_TENANTS`: UUID de negocios separados por coma. Vacía = el alta
automática está apagada (fail-closed). Solo UUID: el slug de un negocio borrado podría
volver a usarse. Es una variable de entorno y no una opción del panel: ningún admin de un
negocio puede darse permiso de crear negocios.
"""

from __future__ import annotations

import uuid

from core.config import get_settings
from core.db.models import Tenant
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)


def vendedores_configurados() -> frozenset[uuid.UUID]:
    """UUID de la allowlist. Una entrada que no es un UUID se ignora y queda en el log."""
    vendedores: set[uuid.UUID] = set()
    for parte in (get_settings().provisioning_seller_tenants or "").split(","):
        valor = parte.strip()
        if not valor:
            continue
        try:
            vendedores.add(uuid.UUID(valor))
        except ValueError:
            log.warning("provisioning_vendedor_invalido", entrada=valor[:64])
    return frozenset(vendedores)


async def es_vendedor(tenant_id: uuid.UUID) -> bool:
    """¿El negocio puede vender negocios? Está en la allowlist, activo y sin borrar.
    `tenants` es tabla de sistema: se lee sin depender de la RLS."""
    if tenant_id not in vendedores_configurados():
        return False
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    return tenant is not None and tenant.deleted_at is None and tenant.status == "active"


__all__ = ["es_vendedor", "vendedores_configurados"]
