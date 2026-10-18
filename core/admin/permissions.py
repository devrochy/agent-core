"""Autorización por sección del panel (RBAC configurable, ADR-0047, ADR-0136).

Fuente única de verdad de "qué puede hacer un admin en el panel". Resuelve el
acceso efectivo combinando, por orden de prioridad:

1. `Admin.role == "master"` → bypass total (el tester habilitado, ADR-0134, igual).
2. Permiso explícito en `admin_permissions` para (admin, sección), global
   (`tenant_id is null`) o del tenant activo. Desde ADR-0136 hay tres valores:
   `read`, `write` y `none` (denegado, solo por negocio), y la fila de una sub-sección
   (`pack_cobranza:reportes`) prevalece sobre la del pack completo (`pack_cobranza`).
   Dentro de un nivel, `none` deniega aunque exista un grant; si no, gana el máximo
   (`write > read`).
3. Rol por defecto del `tenant_user` del tenant activo: `owner` → `write`,
   `colaborador` → `read`, salvo en las secciones de plataforma (`tenants`) y en
   las de gestión de cuentas (`collaborators`), que exigen owner o master.

El catálogo de secciones vive en código (no en tabla): las del core más una por
pack (el `id` del plugin) y sus sub-secciones (ADR-0063). La resolución es una función
pura (`resolver_seccion`) que comparten `effective_access`, `permissions_for` y
`notification_recipients`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping

from fastapi import Request
from sqlalchemy import select

from core.admin.roles import acceso_de_plataforma, es_master, es_tester
from core.db.models import Admin, AdminPermission, TenantUser
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id

# Secciones del core. Deben coincidir con los `section` de `views.py` y el menú.
CORE_SECTIONS: dict[str, str] = {
    "dashboard": "Dashboard",
    "leads": "Leads",
    "conversations": "Conversaciones",
    "escalations": "Escalaciones",
    "connections": "Conexiones",
    "plugins": "Plugins",
    "costs": "Costos LLM",
    "plan": "Plan y consumo",
    "settings": "Configuración",
    "tenants": "Tenants",
    "collaborators": "Colaboradores",
    # Centro de pruebas de la plataforma (SPEC-30, ADR-0133): global, de master y testers.
    "qa": "Pruebas",
}

# Secciones que NUNCA se heredan del rol del `tenant_user`: son de master o de
# permiso explícito (gestión de plataforma).
GLOBAL_ONLY_SECTIONS: frozenset[str] = frozenset({"tenants", "qa"})

# Secciones que solo el `owner` del tenant (o master) puede usar: el `colaborador`
# no las hereda. Gestionar cuentas no es una capacidad de colaborador.
OWNER_ONLY_SECTIONS: frozenset[str] = frozenset({"collaborators"})

# Secciones que un `owner` puede otorgar a un colaborador de su tenant: el
# catálogo menos las de plataforma y la propia gestión de cuentas.
OWNER_GRANTABLE_SECTIONS: frozenset[str] = GLOBAL_ONLY_SECTIONS | OWNER_ONLY_SECTIONS

ACCESS_NONE = "none"
VALID_ACCESS: tuple[str, ...] = ("read", "write", ACCESS_NONE)
_ACCESS_RANK = {ACCESS_NONE: 0, "read": 1, "write": 2}


def seccion_padre(section: str) -> str | None:
    """`pack_cobranza` para `pack_cobranza:reportes`; `None` si la sección no es sub-sección."""
    pack, sep, _sub = section.partition(":")
    return pack if sep else None


def resolver_explicito(
    explicitos: Mapping[str, Iterable[str]], section: str
) -> tuple[bool, str | None]:
    """Acceso explícito de una sección: `(hay_fila, acceso)`.

    `explicitos` mapea sección → accesos de las filas aplicables (global y del tenant
    activo). El nivel más específico gana (la sub-sección antes que el pack); dentro de un
    nivel, `none` deniega sobre cualquier grant; si no, el máximo. Sin filas en ningún
    nivel devuelve `(False, None)` y el llamador cae al default del rol.
    """
    for key in (section, seccion_padre(section)):
        if key is None:
            continue
        accesos = [a for a in (explicitos.get(key) or ()) if a in _ACCESS_RANK]
        if not accesos:
            continue
        if ACCESS_NONE in accesos:
            return True, None
        return True, max(accesos, key=lambda a: _ACCESS_RANK[a])
    return False, None


def acceso_por_rol(section: str, role: str | None, tenant_id: uuid.UUID | None) -> str | None:
    """Default que hereda el rol del `tenant_user` en el tenant activo (ADR-0047 §3)."""
    if tenant_id is None or section in GLOBAL_ONLY_SECTIONS:
        return None
    if role == "owner":
        return "write"
    if role == "colaborador":
        return "read"
    return None


def resolver_seccion(
    section: str,
    role: str | None,
    tenant_id: uuid.UUID | None,
    explicitos: Mapping[str, Iterable[str]],
) -> str | None:
    """Acceso efectivo de un admin que NO es de plataforma (función pura, ADR-0136)."""
    # Gestionar cuentas nunca se hereda por grant ni se deniega por fila: es del `owner`.
    if section in OWNER_ONLY_SECTIONS:
        return "write" if role == "owner" else None
    hay_fila, acceso = resolver_explicito(explicitos, section)
    if hay_fila:
        return acceso
    return acceso_por_rol(section, role, tenant_id)


def catalog(sections: Iterable[str] | None = None) -> dict[str, str]:
    """Catálogo de secciones: core + las que aportan los packs.

    Cada clave es una sección (el `plugin_id` o una **sub-sección** de pack,
    ADR-0063). El label por defecto es la propia clave; el menú usa el label del
    item.
    """
    result = dict(CORE_SECTIONS)
    for section in sections or ():
        result.setdefault(section, section)
    return result


def pack_section_keys(modules: Iterable[object]) -> set[str]:
    """Sub-secciones que aportan los packs (ADR-0063): `section_key` o el `plugin_id`.

    Incluye las sub-secciones de permiso declaradas en `section_keys` (sin item de
    menú), p. ej. `pack_calendar:notificaciones`.
    """
    keys: set[str] = set()
    for module in modules:
        plugin_id = getattr(module, "id", None)
        if not plugin_id:
            continue
        for item in getattr(module, "admin_menu", None) or []:
            keys.add(item.get("section_key") or plugin_id)
        for key in getattr(module, "section_keys", None) or []:
            keys.add(key)
    return keys


async def _filas_explicitas(
    admin_id: uuid.UUID, tenant_id: uuid.UUID | None, secciones: Iterable[str] | None = None
) -> dict[str, list[str]]:
    """Filas de `admin_permissions` aplicables (globales o del tenant activo), por sección."""
    async with get_session() as session:
        where = [
            AdminPermission.admin_id == admin_id,
            AdminPermission.tenant_id.is_(None) | (AdminPermission.tenant_id == tenant_id),
        ]
        if secciones is not None:
            where.append(AdminPermission.section.in_(list(secciones)))
        rows = (
            await session.execute(
                select(AdminPermission.section, AdminPermission.access).where(*where)
            )
        ).all()
    explicitos: dict[str, list[str]] = {}
    for section, access in rows:
        explicitos.setdefault(section, []).append(access)
    return explicitos


async def explicit_access(
    admin_id: uuid.UUID, section: str, tenant_id: uuid.UUID | None
) -> str | None:
    """Acceso explícito (sub-sección > pack; `none` deniega) o `None` si no hay fila o deniega."""
    claves = [section] + ([seccion_padre(section)] if seccion_padre(section) else [])
    _hay, acceso = resolver_explicito(await _filas_explicitas(admin_id, tenant_id, claves), section)
    return acceso


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
    plataforma = acceso_de_plataforma(admin, section)
    if plataforma is not None:
        # Master y tester (ADR-0134): el rol decide; el tester nunca hereda Colaboradores.
        return None if plataforma == "none" else plataforma
    role = await _tenant_user_role(admin.id, tenant_id)
    claves = [section] + ([seccion_padre(section)] if seccion_padre(section) else [])
    explicitos = await _filas_explicitas(admin.id, tenant_id, claves)
    return resolver_seccion(section, role, tenant_id, explicitos)


async def permissions_for(admin: Admin, sections: Iterable[str]) -> dict[str, str | None]:
    """Mapa sección → acceso efectivo del admin en el tenant activo (para el menú)."""
    sections = list(sections)
    if es_master(admin) or es_tester(admin):
        result_plataforma: dict[str, str | None] = {}
        for section in sections:
            acceso = acceso_de_plataforma(admin, section)
            result_plataforma[section] = None if acceso in (None, "none") else acceso
        return result_plataforma

    tenant_id = get_current_tenant_id()
    role = await _tenant_user_role(admin.id, tenant_id)
    explicitos = await _filas_explicitas(admin.id, tenant_id)
    return {section: resolver_seccion(section, role, tenant_id, explicitos) for section in sections}


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
    ("/admin/leads", "leads"),
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
)


def _matches(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def pack_prefixes(modules: Iterable[object]) -> list[tuple[str, str]]:
    """Prefijos de UI admin que aportan los packs, por **item** de menú (ADR-0063).

    Mapea el path COMPLETO de cada item a su `section_key` (sub-sección), no el
    primer segmento al `plugin_id`. `required_section` elige el prefijo más largo,
    así `/cobranza/carteras/123/config` cae en `pack_cobranza:carteras`. Solo las
    rutas que NO viven bajo `/admin` (las de `/admin/packs/<id>` se resuelven por
    segmento, y las de `/admin/plugins/<id>` caen en `plugins`).
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
            prefix = "/" + path.strip("/")
            if prefix != "/" and all(prefix != existing for existing, _ in result):
                result.append((prefix, item.get("section_key") or plugin_id))
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


async def notification_recipients(section: str, tenant_id: uuid.UUID | None) -> list[str]:
    """Emails de los admins con acceso efectivo a `section` en el tenant (HU-CAL-02).

    Usado para copiar los correos de agendamiento a los colaboradores que deben
    enterarse (p. ej. sección `pack_calendar:notificaciones`). Incluye al `owner`
    del tenant (su rol hereda `write`) y a los masters con permiso explícito.
    Tabla de sistema (sin RLS): se consulta con `tenant_id` explícito.
    """
    if tenant_id is None:
        return []
    async with get_session() as session:
        admins = (await session.execute(select(Admin).where(Admin.active.is_(True)))).scalars().all()
        # Rol por tenant (owner hereda write) y permisos explícitos de la sección.
        roles = dict(
            (
                await session.execute(
                    select(TenantUser.admin_id, TenantUser.role).where(
                        TenantUser.tenant_id == tenant_id
                    )
                )
            ).all()
        )
        claves = [section] + ([seccion_padre(section)] if seccion_padre(section) else [])
        explicit = (
            await session.execute(
                select(
                    AdminPermission.admin_id, AdminPermission.section, AdminPermission.access
                ).where(
                    AdminPermission.section.in_(claves),
                    AdminPermission.tenant_id.is_(None) | (AdminPermission.tenant_id == tenant_id),
                )
            )
        ).all()
    # Mismo resolutor explícito que el menú y la API (sub-sección > pack; `none` deniega,
    # ADR-0136). Sin fila decide el rol, pero aquí solo el `owner` recibe copia: un
    # colaborador hereda `read` de la sección, no los avisos.
    por_admin: dict[uuid.UUID, dict[str, list[str]]] = {}
    for admin_id, sec, access in explicit:
        por_admin.setdefault(admin_id, {}).setdefault(sec, []).append(access)
    out: list[str] = []
    for admin in admins:
        if admin.role == "master":
            effective = True
        else:
            hay_fila, acceso = resolver_explicito(por_admin.get(admin.id, {}), section)
            effective = acceso is not None if hay_fila else roles.get(admin.id) == "owner"
        if effective and admin.email:
            out.append(admin.email)
    return out
