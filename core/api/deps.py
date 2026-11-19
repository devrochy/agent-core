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
    NegocioSuspendido,
    admin_from_token,
    bearer_token,
)
from core.admin.permissions import (
    catalog,
    effective_access,
    pack_section_keys,
    permissions_for,
)
from core.admin.roles import actua_como_master
from core.api.errors import ApiError
from core.db.models import Admin
from core.plugin_manager import plugin_manager
from core.shared.tenant import negocios_de_cuenta, tenant_scope


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


def _negocio_suspendido() -> ApiError:
    """403 con código propio: el panel y la app lo distinguen de «sin permiso» (ADR-0136)."""
    return ApiError(403, NegocioSuspendido.code, NegocioSuspendido.mensaje)


async def _resolve_admin(request: Request) -> Admin:
    token = session_token(request)
    if not token:
        raise ApiError.unauthorized()
    admin = await admin_from_token(token)
    if admin is None:
        raise ApiError.unauthorized("Sesión inválida o expirada.")
    return admin


# Rutas que un equipo pendiente o bloqueado sí puede llamar, por método y ruta exacta: registrar/consultar su
# estado, ver quién es y cerrar sesión. `…/gestor-token` es el intercambio a sesión de campo: evalúa el equipo
# él mismo (adopta lo del pack, crea el pendiente, responde con el `code`), así que el principal no puede
# decidir antes que él. Nada de sufijos: una ruta futura con un parámetro final no puede quedar exenta.
_EXENTAS_DE_EQUIPO = frozenset(
    {
        ("POST", "/api/v1/access/device-session"),
        ("GET", "/api/v1/admin/me"),
        ("POST", "/api/v1/pack/cobranza/gestor-token"),
    }
)


def _exenta_de_equipo(method: str, path: str) -> bool:
    return (method, path) in _EXENTAS_DE_EQUIPO or path.startswith("/api/v1/auth/")


async def _exigir_equipo(admin_id: uuid.UUID, tenant_id: uuid.UUID, device_id: str) -> None:
    """403 `{code}` si el equipo no puede operar en este negocio (decisión de solo lectura, con caché)."""
    from core.access.sesion import MENSAJES, evaluar_rapido
    from core.db.session import get_session

    async with get_session() as session:
        code = await evaluar_rapido(session, tenant_id, admin_id, device_id)
    if code is not None:
        raise ApiError(403, code, MENSAJES.get(code, MENSAJES["dispositivo_no_autorizado"]))


async def get_principal(
    request: Request,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
) -> AsyncIterator[Principal]:
    """Resuelve identidad, tenant activo y permisos, y abre el scope de tenant."""
    equipo = (x_device_id or "").strip()[:128]
    token = session_token(request)
    admin = await _resolve_admin(request)
    # Negocios suspendidos fuera, salvo para quien actúa como master (ADR-0136): la sesión de
    # un dueño o colaborador cuyo negocio se suspendió responde 403 `tenant_suspended`.
    negocios = await negocios_de_cuenta(admin)
    tenants = negocios.operativos
    ids = {str(t[0]) for t in tenants}

    if x_tenant_id is not None and x_tenant_id not in ids:
        if x_tenant_id in {str(t) for t in negocios.suspendidos}:
            raise _negocio_suspendido()
        raise ApiError.forbidden("No tienes acceso a ese negocio.")
    if negocios.solo_suspendidos:
        raise _negocio_suspendido()

    target: uuid.UUID | None = None
    if tenants:
        # Header explícito gana; si falta, la cookie (si sigue siendo válida); si
        # no, el primer tenant del admin — mismo criterio que el middleware SSR.
        candidate = x_tenant_id or request.cookies.get(TENANT_COOKIE)
        chosen = candidate if candidate and candidate in ids else str(tenants[0][0])
        target = next(t[0] for t in tenants if str(t[0]) == chosen)

    async with tenant_scope(target):
        # Equipo (ADR-0174): solo si la app manda `X-Device-Id`. El panel web no lo manda y no cambia nada.
        if target is not None and equipo and not _exenta_de_equipo(request.method, request.url.path):
            await _exigir_equipo(admin.id, target, equipo)
        # Sub-secciones de los packs (ADR-0063) más el id de cada pack: así `/me` expone
        # también el acceso «al pack completo» (p. ej. `pack_cobranza`, ADR-0136).
        secciones = pack_section_keys(plugin_manager.list_modules()) | set(plugin_manager.active_ids())
        perms = await permissions_for(admin, catalog(secciones))
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
    """¿Propietario del negocio activo o admin de plataforma (master, o tester en test)?"""
    if actua_como_master(principal.admin):
        return True
    role = next((role for (tid, _name, role) in principal.tenants if tid == principal.tenant_id), None)
    return role == "owner"


def require_owner_or_master(principal: Principal) -> None:
    """Para acciones que cambian lo que atiende a clientes reales (registrar el webhook, BYOK
    de producción, pasar a producción): 403 si no es propietario ni master."""
    if not es_owner_o_master(principal):
        raise ApiError.forbidden("Se requiere ser propietario del negocio o admin de plataforma.")
