"""Política de dispositivos por negocio y rol (ADR-0174 §2).

Vive en `tenants.settings.acceso.dispositivos = {owner, colaborador, gestor}`, cada valor
`abierto` o `restringido`. Sin configurar, rige el default (el gestor conserva el efecto de
ADR-0068; los demás ganan registro y aviso sin que nadie quede fuera).

`tenants.settings` lo reescriben varios caminos enteros: la escritura lee la fila con
`FOR UPDATE` para no pisar una bandera que el master apagó (`tests/test_settings_del_negocio_candado.py`).
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from core.db.models import Tenant

ROLES: tuple[str, ...] = ("owner", "colaborador", "gestor")
ABIERTO = "abierto"
RESTRINGIDO = "restringido"
VALORES: tuple[str, ...] = (ABIERTO, RESTRINGIDO)
DEFAULT: dict[str, str] = {"owner": ABIERTO, "colaborador": ABIERTO, "gestor": RESTRINGIDO}


def resolver(settings: dict[str, Any] | None) -> dict[str, str]:
    """Política efectiva: lo configurado válido, completado con el default por rol."""
    guardada = ((settings or {}).get("acceso") or {}).get("dispositivos") or {}
    return {rol: guardada[rol] if guardada.get(rol) in VALORES else DEFAULT[rol] for rol in ROLES}


async def leer(session: Any, tenant_id: uuid.UUID) -> dict[str, str]:
    tenant = await session.get(Tenant, tenant_id)
    return resolver(tenant.settings if tenant is not None else None)


async def escribir(
    session: Any, tenant_id: uuid.UUID, cambios: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """Aplica `cambios` (rol → valor) y devuelve (antes, después). Lee la fila con candado."""
    for rol, valor in cambios.items():
        if rol not in ROLES or valor not in VALORES:
            raise ValueError(f"política inválida: {rol}={valor}")
    tenant = await session.get(Tenant, tenant_id, with_for_update=True)
    if tenant is None:
        raise LookupError("negocio no encontrado")
    antes = resolver(tenant.settings)
    despues = {**antes, **cambios}
    settings = copy.deepcopy(tenant.settings or {})
    settings.setdefault("acceso", {})["dispositivos"] = despues
    tenant.settings = settings
    return antes, despues
