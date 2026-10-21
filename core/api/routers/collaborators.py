"""Colaboradores (lectura): `GET /api/v1/admin/collaborators`.

Cuentas admin con sus permisos por sección y sus vínculos a tenants. Un `master`
ve todas las cuentas; un `owner` solo las exclusivas de su tenant (mismos datos
que la vista SSR `/admin/colaboradores`). Requiere la sección `collaborators`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from core.admin.collaborators_service import (
    audit,
    create_collaborator,
    grant_permission,
    link_tenant,
    pack_collaborator_roles,
    reset_password,
    resolve_tenant_id,
    revoke_permission,
    secciones_del_catalogo,
    set_permissions,
    toggle_active,
    unlink_tenant,
)
from core.admin.permissions import acceso_y_origen
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.plan_limits import plan_limit_error, plan_limit_responses
from core.billing.enforcement import QuotaExceeded
from core.db.models import Admin, AdminPermission, Tenant, TenantUser
from core.db.session import get_session

router = APIRouter(prefix="/admin/collaborators", tags=["admin · colaboradores"])


class PermissionItem(BaseModel):
    section: str
    access: str
    scope: str


class TenantLink(BaseModel):
    role: str
    slug: str
    name: str


class CollaboratorItem(BaseModel):
    id: uuid.UUID
    email: str
    username: str | None
    role: str
    active: bool
    is_self: bool
    is_platform_master: bool
    permissions: list[PermissionItem]
    links: list[TenantLink]


class PackRole(BaseModel):
    value: str
    label: str


@router.get("/pack-roles", response_model=list[PackRole])
async def list_pack_roles(
    principal: Annotated[Principal, Depends(require("collaborators"))],
) -> list[PackRole]:
    """Roles de colaborador que aportan los packs habilitados (ADR-0066)."""
    return [PackRole(**r) for r in await pack_collaborator_roles()]


@router.get("", response_model=list[CollaboratorItem])
async def list_collaborators(
    principal: Annotated[Principal, Depends(require("collaborators"))],
) -> list[CollaboratorItem]:
    me = principal.admin
    is_master = me.role == "master"
    tid = principal.tenant_id

    async with get_session() as session:
        if is_master:
            admins = (await session.execute(select(Admin).order_by(Admin.email))).scalars().all()
        else:
            admins = list(
                (
                    await session.execute(
                        select(Admin)
                        .join(TenantUser, TenantUser.admin_id == Admin.id)
                        .where(TenantUser.tenant_id == tid)
                        .order_by(Admin.email)
                    )
                )
                .scalars()
                .all()
            )

        admin_ids = [a.id for a in admins]
        perms = []
        links = []
        if admin_ids:
            perm_where = [AdminPermission.admin_id.in_(admin_ids)]
            link_where = [TenantUser.admin_id.in_(admin_ids)]
            if not is_master:
                # Un owner solo ve el scope de SU tenant.
                perm_where.append(AdminPermission.tenant_id == tid)
                link_where.append(TenantUser.tenant_id == tid)
            perms = (
                await session.execute(
                    select(
                        AdminPermission.admin_id,
                        AdminPermission.section,
                        AdminPermission.access,
                        Tenant.slug,
                    )
                    .join(Tenant, Tenant.id == AdminPermission.tenant_id, isouter=True)
                    .where(*perm_where)
                )
            ).all()
            links = (
                await session.execute(
                    select(TenantUser.admin_id, TenantUser.role, Tenant.slug, Tenant.name)
                    .join(Tenant, Tenant.id == TenantUser.tenant_id)
                    .where(*link_where)
                    .order_by(Tenant.name)
                )
            ).all()

    perms_by: dict[uuid.UUID, list[PermissionItem]] = {}
    for admin_id, section, access, slug in perms:
        perms_by.setdefault(admin_id, []).append(
            PermissionItem(section=section, access=access, scope=slug or "*")
        )
    links_by: dict[uuid.UUID, list[TenantLink]] = {}
    for admin_id, role, slug, name in links:
        links_by.setdefault(admin_id, []).append(TenantLink(role=role, slug=slug, name=name))

    return [
        CollaboratorItem(
            id=a.id,
            email=a.email,
            username=a.username,
            role=a.role,
            active=a.active,
            is_self=a.id == me.id,
            is_platform_master=a.role == "master",
            permissions=sorted(
                perms_by.get(a.id, []), key=lambda p: (p.section, p.scope)
            ),
            links=links_by.get(a.id, []),
        )
        for a in admins
    ]


# ------------------------------------------------------------------ acciones
class CollaboratorCreate(BaseModel):
    email: str
    password: str
    role: str = "colaborador"
    username: str = ""
    tenant: str = ""
    pack_roles: list[str] = []


class PasswordReset(BaseModel):
    password: str


class GrantRequest(BaseModel):
    section: str
    access: str = "read"
    tenant: str = ""


class RevokeRequest(BaseModel):
    section: str
    tenant: str = ""


class LinkRequest(BaseModel):
    tenant: str
    role: str = "colaborador"


class UnlinkRequest(BaseModel):
    tenant: str


class SetPermissionsRequest(BaseModel):
    tenant: str = ""
    permissions: dict[str, str]


class PermissionState(BaseModel):
    section: str
    access: str
    heredado: bool


def _fail(error: str | None) -> None:
    if error is not None:
        raise ApiError.bad_request(error, "collaborator_error")


@router.post("", status_code=200, responses=plan_limit_responses("colaboradores"))
async def create_collaborator_endpoint(
    payload: CollaboratorCreate,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> JSONResponse:
    """Alta de una cuenta nueva o invitación de una existente (ADR-0081). Al tope de
    colaboradores del plan, 409 `plan_limit_colaboradores` y no se crea ni se invita a nadie
    (D-35, ADR-0142)."""
    try:
        resultado = await create_collaborator(
            actor=principal.admin,
            email=payload.email,
            password=payload.password,
            role=payload.role,
            username=payload.username,
            tenant_slug=payload.tenant,
            pack_roles=payload.pack_roles,
        )
    except QuotaExceeded as exc:
        raise plan_limit_error(exc) from None
    if isinstance(resultado, str):
        raise ApiError.bad_request(resultado, "collaborator_error")
    audit(
        "create_collaborator",
        principal.admin.email,
        payload.email,
        role=payload.role,
        tenant=payload.tenant or "*",
        resultado=resultado.estado,
    )
    return JSONResponse({"resultado": resultado.estado})


@router.post("/{admin_id}/password", status_code=204)
async def reset_collaborator_password(
    admin_id: uuid.UUID,
    payload: PasswordReset,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    error = await reset_password(
        actor=principal.admin, admin_id=admin_id, password=payload.password
    )
    _fail(error)
    audit("reset_password", principal.admin.email, str(admin_id))
    return Response(status_code=204)


@router.post("/{admin_id}/grant", status_code=204)
async def grant_collaborator_permission(
    admin_id: uuid.UUID,
    payload: GrantRequest,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    error = await grant_permission(
        actor=principal.admin,
        admin_id=admin_id,
        section=payload.section,
        access=payload.access,
        tenant_slug=payload.tenant,
    )
    _fail(error)
    audit(
        "grant",
        principal.admin.email,
        str(admin_id),
        section=payload.section,
        access=payload.access,
        scope=payload.tenant or "*",
    )
    return Response(status_code=204)


@router.post("/{admin_id}/revoke", status_code=204)
async def revoke_collaborator_permission(
    admin_id: uuid.UUID,
    payload: RevokeRequest,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    error = await revoke_permission(
        actor=principal.admin,
        admin_id=admin_id,
        section=payload.section,
        tenant_slug=payload.tenant,
    )
    _fail(error)
    audit(
        "revoke",
        principal.admin.email,
        str(admin_id),
        section=payload.section,
        scope=payload.tenant or "*",
    )
    return Response(status_code=204)


@router.get("/{admin_id}/permissions", response_model=list[PermissionState])
async def read_collaborator_permissions(
    admin_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("collaborators"))],
    tenant: str = "",
) -> list[PermissionState]:
    """Acceso efectivo por sección y si es heredado, para la grilla de permisos (D-SPEC-34-1).

    Un owner/tester ve el estado en su negocio activo; el master elige el negocio con `tenant`
    (vacío = alcance global)."""
    me = principal.admin
    if me.role == "master":
        tenant_id = await resolve_tenant_id(tenant) if tenant else None
        if tenant and tenant_id is None:
            raise ApiError.bad_request("tenant no encontrado", "collaborator_error")
    else:
        tenant_id = principal.tenant_id

    async with get_session() as session:
        target = await session.get(Admin, admin_id)
    if target is None:
        raise ApiError.bad_request("admin no encontrado", "collaborator_error")

    estado = await acceso_y_origen(target, tenant_id, secciones_del_catalogo())
    return [PermissionState(section=s, access=a, heredado=h) for s, (a, h) in estado.items()]


@router.post("/{admin_id}/permissions", status_code=204)
async def set_collaborator_permissions(
    admin_id: uuid.UUID,
    payload: SetPermissionsRequest,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    """Aplica en bloque los permisos de un colaborador (grilla, D-SPEC-34-1)."""
    error = await set_permissions(
        actor=principal.admin,
        admin_id=admin_id,
        permissions=payload.permissions,
        tenant_slug=payload.tenant,
    )
    _fail(error)
    audit(
        "set_permissions",
        principal.admin.email,
        str(admin_id),
        scope=payload.tenant or "*",
        secciones=sorted(payload.permissions),
    )
    return Response(status_code=204)


@router.post("/{admin_id}/link", status_code=204)
async def link_collaborator_tenant(
    admin_id: uuid.UUID,
    payload: LinkRequest,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    error = await link_tenant(
        actor=principal.admin,
        admin_id=admin_id,
        tenant_slug=payload.tenant,
        role=payload.role,
    )
    _fail(error)
    audit(
        "link", principal.admin.email, str(admin_id), tenant=payload.tenant, role=payload.role
    )
    return Response(status_code=204)


@router.post("/{admin_id}/unlink", status_code=204)
async def unlink_collaborator_tenant(
    admin_id: uuid.UUID,
    payload: UnlinkRequest,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    """Desvincula la cuenta de un negocio (ADR-0136). Un owner solo de su negocio activo y
    solo a colaboradores; el master, de cualquiera. Nadie deja un negocio sin propietario."""
    error = await unlink_tenant(actor=principal.admin, admin_id=admin_id, tenant_slug=payload.tenant)
    _fail(error)
    audit("unlink", principal.admin.email, str(admin_id), tenant=payload.tenant)
    return Response(status_code=204)


@router.post("/{admin_id}/toggle", status_code=204, responses=plan_limit_responses("colaboradores"))
async def toggle_collaborator_active(
    admin_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("collaborators", write=True))],
) -> Response:
    try:
        error = await toggle_active(
            actor=principal.admin, admin_id=admin_id, current_tenant_id=principal.tenant_id
        )
    except QuotaExceeded as exc:
        # Reactivar una cuenta con el negocio al tope de colaboradores (ADR-0142).
        raise plan_limit_error(exc) from None
    _fail(error)
    audit("toggle", principal.admin.email, str(admin_id))
    return Response(status_code=204)
