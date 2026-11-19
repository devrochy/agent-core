"""Rol de acceso de una cuenta ante la política de equipos (ADR-0174 §2).

`master` (admin de plataforma), `owner`, `gestor` o `colaborador`. Los tres primeros salen del core; `gestor`
lo aporta el pack de cobranza por el hook `PackModule.rol_de_acceso` (el core no importa del pack).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from core.access.devices import rol_de_cuenta
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_TIMEOUT_S = 1.0
# Lo único que un pack puede declarar: más estricto que un colaborador cualquiera, nunca owner ni master.
_ROLES_DE_PACK = frozenset({"gestor", "colaborador"})


async def rol_de_acceso(session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID) -> str | None:
    """Rol de acceso, o `None` si la cuenta no pertenece al negocio.

    Si un pack falla o tarda de más se asume `gestor`, la política más estricta: ante la duda, no se
    abre el acceso."""
    base = await rol_de_cuenta(session, tenant_id, admin_id)
    if base != "colaborador":
        return base
    from core.plugin_manager import plugin_manager

    for modulo in plugin_manager.list_modules():
        try:
            async with tenant_scope(tenant_id):
                rol = await asyncio.wait_for(modulo.rol_de_acceso(tenant_id, admin_id), _TIMEOUT_S)
        except Exception:
            log.warning("access_rol_de_acceso_fallo", pack=getattr(modulo, "id", None), exc_info=True)
            return "gestor"
        if rol is None:
            continue
        if rol not in _ROLES_DE_PACK:
            log.warning("access_rol_de_pack_invalido", pack=getattr(modulo, "id", None), rol=str(rol))
            return "gestor"
        return rol
    return base
