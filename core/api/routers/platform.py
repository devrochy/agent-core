"""Plataforma (master): `GET /api/v1/admin/platform/tenants` y la App de Meta.

Listado **de todos** los tenants para gestión de plataforma, con su plan. Solo
para el admin `master`. Paginado (la vista SSR recorre todos y calcula el plan de
cada uno; aquí solo de la página) y con búsqueda por nombre/slug.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from core.admin.plan_view import subscription_overview_or_none
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params
from core.api.schemas import ReadinessRun, readiness_run_out
from core.db.models import Tenant
from core.db.session import get_session

router = APIRouter(prefix="/admin/platform", tags=["admin · plataforma"])


def _require_master(principal: Principal) -> None:
    if principal.admin.role != "master":
        raise ApiError.forbidden("Solo el admin de plataforma puede gestionar negocios.")


class PlatformTenant(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    plan_name: str
    plan_status: str
    operational: bool


@router.get("/tenants", response_model=Page[PlatformTenant])
async def list_platform_tenants(
    principal: Annotated[Principal, Depends(get_principal)],
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(description="Busca por nombre o slug.")] = None,
) -> Page[PlatformTenant]:
    _require_master(principal)

    query = select(Tenant).where(Tenant.deleted_at.is_(None))
    if q:
        like = f"%{q}%"
        query = query.where(or_(Tenant.name.ilike(like), Tenant.slug.ilike(like)))

    async with get_session() as session:
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar() or 0
        rows = (
            await session.execute(
                query.order_by(Tenant.created_at)
                .offset(params.offset)
                .limit(params.page_size)
            )
        ).scalars().all()

    items: list[PlatformTenant] = []
    for t in rows:
        overview = await subscription_overview_or_none(t.id)
        items.append(
            PlatformTenant(
                id=t.id,
                name=t.name,
                slug=t.slug,
                status=t.status,
                created_at=t.created_at,
                plan_name=overview.plan_name if overview else "—",
                plan_status=overview.status if overview else "sin_suscripcion",
                operational=overview.operational if overview else True,
            )
        )
    return build_page(items, int(total), params)


class TenantCreate(BaseModel):
    name: str
    slug: str = ""
    business: str = ""
    language: str = "es"
    tone: str = "cercano"
    packs: list[str] = []
    escalation_triggers: list[str] = []
    admin_email: str = ""
    admin_password: str = ""


class TenantPlanUpdate(BaseModel):
    plan_id: str
    extend_days: int = 0


class PurgeRequest(BaseModel):
    confirm_phrase: str


class TenantCreated(BaseModel):
    tenant_id: uuid.UUID
    slug: str
    steps: list[str]


@router.post("/tenants", response_model=TenantCreated, status_code=201)
async def create_tenant(
    payload: TenantCreate,
    principal: Annotated[Principal, Depends(get_principal)],
) -> TenantCreated:
    """Alta de un negocio (master): tenant + asistente + cuenta admin opcional."""
    _require_master(principal)

    # Import local: la lógica de alta vive en el core; el slug se normaliza igual.
    from core.admin.views import _slugify
    from core.alta_negocio import onboard_tenant

    slug = payload.slug.strip() or _slugify(payload.name)
    if not payload.name.strip() or not slug:
        raise ApiError.bad_request("Nombre y slug son obligatorios.")
    admin_email = payload.admin_email.strip().lower()
    if admin_email and len(payload.admin_password) < 8:
        raise ApiError.bad_request("La contraseña del administrador necesita 8+ caracteres.")

    try:
        tenant_id, steps = await onboard_tenant(
            name=payload.name.strip(),
            slug=slug,
            business_description=payload.business,
            language=payload.language,
            persona={"tone": payload.tone},
            escalation_triggers=payload.escalation_triggers,
            packs=payload.packs,
            admin_email=admin_email or None,
            admin_password=payload.admin_password or None,
        )
    except ValueError as exc:
        raise ApiError.bad_request(str(exc)[:180], "invalid_tenant") from exc
    return TenantCreated(tenant_id=tenant_id, slug=slug, steps=steps)


@router.post("/tenants/{tenant_id}/plan", status_code=204)
async def set_tenant_plan(
    tenant_id: uuid.UUID,
    payload: TenantPlanUpdate,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    """Cambia el plan de un negocio (soporte, master) y lo audita (ADR-0045)."""
    _require_master(principal)

    from datetime import timedelta

    from core.billing.plans import get_plan
    from core.billing.service import ensure_free_subscription
    from core.db.models import AuditLog, TenantSubscription
    from core.shared.tenant import tenant_scope

    if get_plan(payload.plan_id) is None:
        raise ApiError.bad_request("Plan desconocido.", "unknown_plan")

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == tenant_id,
                        TenantSubscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if sub is None:
                sub = await ensure_free_subscription(session, tenant_id)
            sub.plan_id = payload.plan_id
            sub.status = "active"
            if payload.extend_days:
                from core.db.models import utcnow

                base = sub.period_end if sub.period_end and sub.period_end > utcnow() else utcnow()
                sub.period_end = base + timedelta(days=payload.extend_days)
                sub.trial_ends_at = None
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor=f"admin:{principal.admin.email}",
                    action="billing.set_plan",
                    input={"plan_id": payload.plan_id, "extend_days": payload.extend_days},
                )
            )
    return Response(status_code=204)


@router.post("/tenants/{tenant_id}/activate", status_code=204)
async def activate_tenant_endpoint(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    _require_master(principal)
    from core.tenants import activate_tenant

    await activate_tenant(str(tenant_id))
    return Response(status_code=204)


@router.post("/tenants/{tenant_id}/deactivate", status_code=204)
async def deactivate_tenant_endpoint(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    _require_master(principal)
    from core.tenants import deactivate_tenant

    await deactivate_tenant(str(tenant_id))
    return Response(status_code=204)


@router.post("/tenants/{tenant_id}/purge", status_code=204)
async def purge_tenant_endpoint(
    tenant_id: uuid.UUID,
    payload: PurgeRequest,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    """Eliminación física IRREVERSIBLE: exige la frase `ELIMINAR <slug>` (invariante 8)."""
    _require_master(principal)
    from core.tenants import purge_tenant

    try:
        await purge_tenant(str(tenant_id), payload.confirm_phrase)
    except ValueError as exc:
        raise ApiError.bad_request(str(exc)[:180], "purge_confirmation") from exc
    except Exception as exc:
        raise ApiError(502, "purge_failed", f"No se pudo eliminar: {str(exc)[:140]}") from exc
    return Response(status_code=204)


# ------------------------------------------------- App de Meta de la plataforma (HU-CONN-07)
class PlatformMetaApp(ReadinessRun):
    """Checklist de la app de Meta de la plataforma y la infraestructura (solo master)."""

    meta_app_id: str = ""


async def _platform_meta_app(principal: Principal) -> PlatformMetaApp:
    from core.admin.production_readiness_service import latest_platform_run
    from core.config import get_settings

    run = await latest_platform_run(principal.admin)
    return PlatformMetaApp(meta_app_id=get_settings().meta_app_id or "", **readiness_run_out(run))


@router.get("/meta-app", response_model=PlatformMetaApp)
async def get_platform_meta_app(
    principal: Annotated[Principal, Depends(get_principal)],
) -> PlatformMetaApp:
    """Último resultado guardado (con fecha) de los chequeos de la app de Meta de la
    plataforma e infraestructura: webhook, suscripciones de la app, App Secret, claves de
    cifrado, worker, RLS. No corre nada: para revisar, `POST /meta-app/run`."""
    _require_master(principal)
    return await _platform_meta_app(principal)


@router.post("/meta-app/run", response_model=PlatformMetaApp, status_code=202)
async def run_platform_meta_app(
    principal: Annotated[Principal, Depends(get_principal)],
) -> PlatformMetaApp:
    """Encola la revisión de la plataforma (job de Celery, sin pytest) y responde al instante
    con la corrida `queued` (o la que ya estaba en curso)."""
    from core.admin import production_readiness_service as readiness

    _require_master(principal)
    try:
        await readiness.request_platform_run(principal.admin)
    except readiness.EnqueueFailed as exc:
        raise ApiError(503, "enqueue_failed", str(exc)) from exc
    return await _platform_meta_app(principal)
