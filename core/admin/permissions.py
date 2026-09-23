"""Autorización por sección del panel (RBAC configurable, ADR-0047).

Fuente única de verdad de "qué puede hacer un admin en el panel". Resuelve el
acceso efectivo combinando, por orden de prioridad:

1. `Admin.role == "master"` → bypass total.
2. Permiso explícito en `admin_permissions` para (admin, sección), global
   (`tenant_id is null`) o del tenant activo; gana el máximo (`write > read`).
3. Rol por defecto del `tenant_user` del tenant activo: `owner` → `write`,
   `colaborador` → `read`, salvo en las secciones de plataforma (`tenants`) y en
   las de gestión de cuentas (`collaborators`), que exigen owner o master.

El catálogo de secciones vive en código (no en tabla): las del core más una por
pack, con el `id` del plugin como clave.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from fastapi import Request
from sqlalchemy import select

from core.db.models import Admin, AdminPermission, TenantUser
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

# Secciones del core. Deben coincidir con los `section` de `views.py` y el menú.
CORE_SECTIONS: dict[str, str] = {
    "dashboard": "Dashboard",
    "conversations": "Conversaciones",
    "escalations": "Escalaciones",
    "connections": "Conexiones",
    "plugins": "Plugins",
    "costs": "Costos LLM",
    "plan": "Plan y consumo",
    "settings": "Configuración",
    "tenants": "Tenants",
    "collaborators": "Colaboradores",
}

# Secciones que NUNCA se heredan del rol del `tenant_user`: son de master o de
# permiso explícito (gestión de plataforma).
GLOBAL_ONLY_SECTIONS: frozenset[str] = frozenset({"tenants"})

# Secciones que solo el `owner` del tenant (o master) puede usar: el `colaborador`
# no las hereda. Gestionar cuentas no es una capacidad de colaborador.
OWNER_ONLY_SECTIONS: frozenset[str] = frozenset({"collaborators"})

# Secciones que un `owner` puede otorgar a un colaborador de su tenant: el
# catálogo menos las de plataforma y la propia gestión de cuentas.
OWNER_GRANTABLE_SECTIONS: frozenset[str] = GLOBAL_ONLY_SECTIONS | OWNER_ONLY_SECTIONS

_ACCESS_RANK = {"read": 1, "write": 2}


def catalog(plugin_ids: Iterable[str] | None = None) -> dict[str, str]:
    """Catálogo de secciones: core + una por pack (id del plugin como clave)."""
    result = dict(CORE_SECTIONS)
    for plugin_id in plugin_ids or ():
        result.setdefault(plugin_id, plugin_id)
    return result


def _best(current: str | None, candidate: str) -> str:
    if current is None or _ACCESS_RANK.get(candidate, 0) > _ACCESS_RANK.get(current, 0):
        return candidate
    return current


async def explicit_access(
    admin_id: uuid.UUID, section: str, tenant_id: uuid.UUID | None
) -> str | None:
    """Máximo acceso explícito entre el grant global y el del tenant activo."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(AdminPermission.access).where(
                    AdminPermission.admin_id == admin_id,
                    AdminPermission.section == section,
                    AdminPermission.tenant_id.is_(None) | (AdminPermission.tenant_id == tenant_id),
                )
            )
        ).scalars().all()
    best: str | None = None
    for access in rows:
        best = _best(best, access)
    return best


async def _tenant_user_role(admin_id: uuid.UUID, tenant_id: uuid.UUID | None) -> str | None:
    if tenant_id is None:
        return None
    async with get_session() as session:
        return (
            await session.execute(
                select(TenantUser.role).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()


async def effective_access(
    admin: Admin, section: str, tenant_id: uuid.UUID | None
) -> str | None:
    """Acceso efectivo del admin a una sección en el tenant activo (`None` = denegado)."""
    if admin.role == "master":
        return "write"
    role = await _tenant_user_role(admin.id, tenant_id)
    # Gestionar cuentas nunca se hereda por grant: es del `owner` del tenant.
    if section in OWNER_ONLY_SECTIONS:
        return "write" if role == "owner" else None
    explicit = await explicit_access(admin.id, section, tenant_id)
    if explicit is not None:
        return explicit
    if tenant_id is None or section in GLOBAL_ONLY_SECTIONS:
        return None
    if role == "owner":
        return "write"
    if role == "colaborador":
        return "read"
    return None


async def permissions_for(admin: Admin, sections: Iterable[str]) -> dict[str, str | None]:
    """Mapa sección → acceso efectivo del admin en el tenant activo (para el menú)."""
    sections = list(sections)
    if admin.role == "master":
        return {section: "write" for section in sections}

    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        rows = (
            await session.execute(
                select(AdminPermission.section, AdminPermission.access).where(
                    AdminPermission.admin_id == admin.id,
                    AdminPermission.tenant_id.is_(None) | (AdminPermission.tenant_id == tenant_id),
                )
            )
        ).all()
        role = None
        if tenant_id is not None:
            role = (
                await session.execute(
                    select(TenantUser.role).where(
                        TenantUser.admin_id == admin.id, TenantUser.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()

    explicit: dict[str, str] = {}
    for section, access in rows:
        explicit[section] = _best(explicit.get(section), access)

    default: str | None = None
    if role == "owner":
        default = "write"
    elif role == "colaborador":
        default = "read"

    result: dict[str, str | None] = {}
    for section in sections:
        if section in OWNER_ONLY_SECTIONS:
            result[section] = "write" if role == "owner" else None
        elif section in explicit:
            result[section] = explicit[section]
        elif default is None or section in GLOBAL_ONLY_SECTIONS:
            result[section] = None
        else:
            result[section] = default
    return result


async def require_section(request: Request, section: str, *, write: bool = False) -> bool:
    """`True` si el admin de la sesión puede acceder (y escribir, si `write`)."""
    from core.admin.auth import current_admin

    admin = await current_admin(request)
    if admin is None:
        return False
    access = await effective_access(admin, section, get_current_tenant_id())
    if access is None:
        return False
    if write and access != "write":
        return False
    return True


# ------------------------------------------------------------------ rutas → sección
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Rutas del panel exentas del gate por sección (auth y selector de tenant).
_SKIP_PREFIXES = ("/admin/login", "/admin/logout", "/admin/switch-tenant")

_CORE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/admin/tenants", "tenants"),
    ("/admin/colaboradores", "collaborators"),
    ("/admin/admins", "collaborators"),
    ("/admin/conversaciones", "conversations"),
    ("/admin/escalaciones", "escalations"),
    ("/admin/conexiones", "connections"),
    # Pestañas de configuración aportadas por packs (ADR-0049): van bajo el
    # prefijo del plugin pero pertenecen a la sección "settings".
    ("/admin/plugins/pack_payments/config", "settings"),
    ("/admin/plugins", "plugins"),
    ("/admin/spec", "settings"),
    ("/admin/costos", "costs"),
    ("/admin/plan", "plan"),
    ("/admin/configuracion", "settings"),
    ("/admin/dlq", "settings"),
)


def _matches(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def pack_prefixes(modules: Iterable[object]) -> list[tuple[str, str]]:
    """Prefijos de UI admin que aportan los packs (ej. `/crm` → `pack_commerce`).

    Solo las rutas de menú que NO viven bajo `/admin` (las de `/admin/packs/<id>`
    se resuelven por segmento, y las de `/admin/plugins/<id>` caen en `plugins`).
    """
    result: list[tuple[str, str]] = []
    for module in modules:
        plugin_id = getattr(module, "id", None)
        if not plugin_id:
            continue
        for item in getattr(module, "admin_menu", None) or []:
            path = item.get("path", "")
            if not path or path.startswith("/admin"):
                continue
            segment = "/" + path.strip("/").split("/")[0]
            if segment != "/" and all(segment != existing for existing, _ in result):
                result.append((segment, plugin_id))
    return result


def required_section(
    path: str, method: str, pack_prefixes: Iterable[tuple[str, str]] | None = None
) -> tuple[str, bool] | None:
    """Sección (y si exige escritura) de una ruta; `None` = sin gate de sección."""
    if path.startswith(_SKIP_PREFIXES):
        return None
    write = method.upper() in _WRITE_METHODS

    candidates = list(pack_prefixes or ())
    if path.startswith("/admin/packs/"):
        parts = path.split("/")
        if len(parts) >= 4 and parts[3]:
            candidates.append((f"/admin/packs/{parts[3]}", parts[3]))
    for prefix, section in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
        if _matches(path, prefix):
            return section, write

    if path.startswith("/admin"):
        for prefix, section in _CORE_PREFIXES:
            if _matches(path, prefix):
                return section, write
        return "dashboard", write
    return None
