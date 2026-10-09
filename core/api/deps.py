"""Dependencias de la API `/api/v1`: identidad, tenant y autorización (ADR-0077).

Un `Principal` es la identidad resuelta del request:

- **Autenticación:** cookie `agent_core_session` (panel web) o `Authorization:
  Bearer <token>` (apps). Ambos son el mismo token opaco de `AdminSession`, con
  revocación inmediata. El realm gestor (JWT de pack) llega en un slice posterior.
- **Tenant activo:** `X-Tenant-Id` (header) o la cookie `ac_tenant`; validado
  contra los tenants del admin. Nunca se toma del cuerpo del request.
- **Autorización:** reusa `core.admin.permissions` (RBAC por sección, ADR-0047).

El scope de tenant se abre en el yield de `get_principal`, así que `get_session()`
dentro del endpoint sigue aplicando RLS.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from core.admin.auth import (
    SESSION_COOKIE,
    TENANT_COOKIE,
    admin_from_token,
    bearer_token,
)
from core.admin.permissions import (
    catalog,
    effective_access,
    pack_section_keys,
    permissions_for,
)
from core.api.errors import ApiError
from core.db.models import Admin
from core.plugin_manager import plugin_manager
from core.shared.tenant import admin_tenants, tenant_scope


@dataclass(slots=True)
class Principal:
    """Identidad resuelta del request."""

    admin: Admin
    tenant_id: uuid.UUID | None
    tenants: list[tuple[uuid.UUID, str, str]]
    permissions: dict[str, str | None]
    token: str | None = None


def session_token(request: Request) -> str | None:
    """Token de sesión del request: cookie `httpOnly` o `Authorization: Bearer`."""
    return request.cookies.get(SESSION_COOKIE) or bearer_token(request)


async def _resolve_admin(request: Request) -> Admin:
    token = session_token(request)
    if not token:
        raise ApiError.unauthorized()
    admin = await admin_from_token(token)
    if admin is None:
        raise ApiError.unauthorized("Sesión inválida o expirada.")
    return admin


async def get_principal(
    request: Request,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> AsyncIterator[Principal]:
    """Resuelve identidad, tenant activo y permisos, y abre el scope de tenant."""
    token = session_token(request)
    admin = await _resolve_admin(request)
    tenants = await admin_tenants(admin.id)
    ids = {str(t[0]) for t in tenants}

    if x_tenant_id is not None and x_tenant_id not in ids:
        raise ApiError.forbidden("No tienes acceso a ese negocio.")

    target: uuid.UUID | None = None
    if tenants:
        # Header explícito gana; si falta, la cookie (si sigue siendo válida); si
        # no, el primer tenant del admin — mismo criterio que el middleware SSR.
        candidate = x_tenant_id or request.cookies.get(TENANT_COOKIE)
        chosen = candidate if candidate and candidate in ids else str(tenants[0][0])
        target = next(t[0] for t in tenants if str(t[0]) == chosen)

    async with tenant_scope(target):
        perms = await permissions_for(
            admin, catalog(pack_section_keys(plugin_manager.list_modules()))
        )
        yield Principal(
            admin=admin,
            tenant_id=target,
            tenants=tenants,
            permissions=perms,
            token=token,
        )


def require(section: str, *, write: bool = False) -> Callable[..., AsyncIterator[Principal]]:
    """Dependencia que exige acceso (y escritura, si `write`) a una sección."""

    async def dependency(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> AsyncIterator[Principal]:
        if principal.admin.role == "master":
            yield principal
            return
        access = await effective_access(principal.admin, section, principal.tenant_id)
        if access is None or (write and access != "write"):
            raise ApiError.forbidden(f"Sin permiso para la sección '{section}'.")
        yield principal

    return dependency


def es_owner_o_master(principal: Principal) -> bool:
    """¿Propietario del negocio activo o admin de plataforma?"""
    if principal.admin.role == "master":
        return True
    role = next((role for (tid, _name, role) in principal.tenants if tid == principal.tenant_id), None)
    return role == "owner"


def require_owner_or_master(principal: Principal) -> None:
    """Para acciones que cambian lo que atiende a clientes reales (registrar el webhook, BYOK
    de producción, pasar a producción): 403 si no es propietario ni master."""
    if not es_owner_o_master(principal):
        raise ApiError.forbidden("Se requiere ser propietario del negocio o admin de plataforma.")
