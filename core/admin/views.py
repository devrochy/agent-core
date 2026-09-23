"""Vistas SSR del panel admin (FastAPI + Jinja2 + Bootstrap 5).

Todo protegido por sesión (cookie). El menú lateral incluye dinámicamente
las entradas de los plugins ACTIVOS (plugin_manager.active_admin_menu()):
al desactivar un plugin su UI desaparece sola.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentcore_contracts import AgentSpec
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from core.admin.auth import (
    SESSION_COOKIE,
    TENANT_COOKIE,
    current_admin,
    hash_password,
    login,
    logout,
)
from core.admin.context import base_context as _base_context
from core.admin.form_generator import (
    parse_form_values,
    parse_json_schema_form,
    render_form_fields,
    render_json_schema_form,
)
from core.admin.permissions import OWNER_GRANTABLE_SECTIONS
from core.admin.permissions import catalog as sections_catalog
from core.admin.view_registry import view_registry
from core.agents.spec import CORE_PACK_ID, enabled_packs, load_active_spec
from core.billing.enforcement import QuotaExceeded, can_activate_plugin
from core.config import get_settings
from core.db.models import (
    WIZARD_STEPS,
    Admin,
    AdminPermission,
    AgentSpecRecord,
    AppSettings,
    Conversation,
    Customer,
    Escalation,
    Message,
    MessageCost,
    MessageTemplate,
    MetaConnection,
    PhoneNumber,
    PluginConfig,
    PluginRecord,
    PluginTenantState,
    PricingDaily,
    Tenant,
    TenantUser,
    UsageLedger,
    WabaAccount,
    utcnow,
)
from core.db.session import get_owned, get_session
from core.gateway.connections import (
    delete_connection,
    list_connections,
    set_active,
    set_primary,
    verify_meta_credentials,
)
from core.llm.difficulty_router import (
    DIFFICULTY_SCORE_THRESHOLD,
    LONG_MESSAGE_THRESHOLD,
    TURNOS_SIN_RESOLVER_THRESHOLD,
)
from core.mcp.host import mcp_host
from core.plugin_manager import plugin_manager
from core.shared.crypto import get_secret_box
from core.shared.exceptions import PluginConfigError, PluginError
from core.shared.logger import get_logger
from core.shared.tenant import admin_tenants, get_current_tenant_id, tenant_scope

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# Respuestas que pueden transportar secretos (p.ej. el token extendido del
# wizard): nunca cachear en navegador/proxies.
_NO_STORE = {"Cache-Control": "no-store"}

INBOUND_STREAM = "agent_core:inbound"
DLQ_STREAM = "agent_core:inbound:dlq"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def _render(name: str, ctx: dict[str, Any], status_code: int = 200):  # type: ignore[no-untyped-def]
    """Starlette >= 0.29: TemplateResponse(request, name, context)."""
    return templates.TemplateResponse(ctx["request"], name, ctx, status_code=status_code)


# ------------------------------------------------------------------ auth
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return _render(
        "login.html",
        {"request": request, "assistant_name": get_settings().app_name, "error": None},
    )


@router.post("/login")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    token = await login(email, password)
    if token is None:
        return _render(
            "login.html",
            {
                "request": request,
                "assistant_name": get_settings().app_name,
                "error": "Credenciales inválidas",
            },
            status_code=401,
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=get_settings().app_env == "production",
    )
    return response


async def _pack_view_allowed(plugin_id: str) -> bool:
    """Enablement del pack para el tenant (ADR-0019): la vista por descriptor solo
    se sirve si el pack está activo y habilitado en el spec del tenant, igual que
    las tools en el orchestrator (`_tool_denial`). RLS aísla los datos igualmente,
    pero un pack no provisionado no debe exponer su UI."""
    from core.agents.spec import enabled_packs, load_active_spec

    if not plugin_manager.is_active(plugin_id):
        return False
    spec = await load_active_spec()
    return plugin_id in enabled_packs(spec)


@router.get("/packs/{plugin_id}/{view}", response_class=HTMLResponse)
async def pack_descriptor_view(request: Request, plugin_id: str, view: str) -> HTMLResponse:
    """Vista por descriptor (Fase D): el pack registra tabla+form+handlers y el
    core sirve el HTML genérico. Sin plantillas ni rutas propias del pack."""
    ctx = await _base_context(request, "packs")
    if ctx is None:
        return _login_redirect()
    if not await _pack_view_allowed(plugin_id):
        return RedirectResponse(
            "/admin?flash=pack no habilitado para este tenant&flash_type=danger", status_code=303
        )
    entry = view_registry.get(plugin_id, view)
    if entry is None:
        return RedirectResponse("/admin", status_code=303)
    descriptor, list_handler, _save = entry
    rows = await list_handler()
    ctx.update(
        descriptor=descriptor,
        rows=rows,
        form_fields=render_json_schema_form(descriptor.form),
        view_path=f"/admin/packs/{plugin_id}/{descriptor.path.strip('/')}",
    )
    return _render("view_descriptor.html", ctx)


@router.post("/packs/{plugin_id}/{view}/guardar")
async def pack_descriptor_save(request: Request, plugin_id: str, view: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return _login_redirect()
    if not await _pack_view_allowed(plugin_id):
        return RedirectResponse(
            "/admin?flash=pack no habilitado para este tenant&flash_type=danger", status_code=303
        )
    entry = view_registry.get(plugin_id, view)
    if entry is None:
        return RedirectResponse("/admin", status_code=303)
    descriptor, _list, save_handler = entry
    form = await request.form()
    form_data = {str(k): str(v) for k, v in form.items()}
    record_id = form.get("record_id")
    values = parse_json_schema_form(descriptor.form, form_data)
    await save_handler(str(record_id) if record_id else None, values)
    return RedirectResponse(
        f"/admin/packs/{plugin_id}/{descriptor.path.strip('/')}?flash=Guardado&flash_type=success",
        status_code=303,
    )


@router.post("/switch-tenant")
async def switch_tenant(request: Request, tenant_id: str = Form(...)) -> RedirectResponse:
    """Selector de tenant del panel (C3). Solo permite tenants del admin; un
    tenant ajeno se ignora (fallback al primero) — sin escalada de acceso."""
    admin = await current_admin(request)
    if admin is None:
        return _login_redirect()
    tenants = await admin_tenants(admin.id)
    if any(str(t[0]) == tenant_id for t in tenants):
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            TENANT_COOKIE,
            tenant_id,
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
            secure=get_settings().app_env == "production",
        )
        return response
    return RedirectResponse(
        "/admin?flash=tenant_sin_acceso&flash_type=danger", status_code=303
    )


@router.post("/logout")
async def logout_submit(request: Request) -> RedirectResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await logout(token)
    response = _login_redirect()
    response.delete_cookie(SESSION_COOKIE)
    return response


# ------------------------------------------------------------------ Tenants (onboarding, ADR-0001)
def _slugify(value: str) -> str:
    import re
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-") or "tenant"


async def _require_master(request: Request) -> bool:
    admin = await current_admin(request)
    if admin is None or admin.role != "master":
        return False
    return True


@router.get("/tenants", response_class=HTMLResponse)
async def tenants_list(request: Request):
    ctx = await _base_context(request, "tenants")
    if ctx is None:
        return _login_redirect()
    if not await _require_master(request):
        return RedirectResponse("/admin?flash=requiere_admin_master&flash_type=danger", status_code=303)

    from core.db.models import Tenant

    async with get_session() as session:
        rows = (
            await session.execute(select(Tenant).order_by(Tenant.created_at))
        ).scalars().all()
    from core.admin.plan_view import subscription_overview_or_none

    tenant_rows = []
    for t in rows:
        overview = await subscription_overview_or_none(t.id)
        tenant_rows.append(
            {
                "id": str(t.id),
                "name": t.name,
                "slug": t.slug,
                "status": t.status,
                "created_at": t.created_at,
                "plan_name": overview.plan_name if overview else "—",
                "plan_status": overview.status if overview else "sin_suscripcion",
                "operational": overview.operational if overview else True,
            }
        )
    ctx["tenants_rows"] = tenant_rows
    from core.billing.plans import public_plans

    ctx["plans"] = [
        {"id": p.id, "name": p.name, "is_free": p.is_free} for p in public_plans()
    ]
    return _render("tenants.html", ctx)


@router.post("/tenants/{tenant_id}/plan")
async def tenants_set_plan(
    request: Request,
    tenant_id: str,
    plan_id: str = Form(...),
    extend_days: int = Form(default=0),
):
    """Cambia el plan de un tenant (soporte, solo master) y registra en audit_log (ADR-0045)."""
    if await current_admin(request) is None:
        return _login_redirect()
    if not await _require_master(request):
        return RedirectResponse("/admin?flash=requiere_admin_master&flash_type=danger", status_code=303)

    from datetime import timedelta

    from core.db.models import AuditLog, TenantSubscription

    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        return RedirectResponse("/admin/tenants?flash=Tenant inválido&flash_type=danger", status_code=303)

    from core.billing.plans import get_plan

    if get_plan(plan_id) is None:
        return RedirectResponse("/admin/tenants?flash=Plan desconocido&flash_type=danger", status_code=303)

    async with tenant_scope(str(tid)):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == tid,
                        TenantSubscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if sub is None:
                from core.billing.service import ensure_free_subscription

                sub = await ensure_free_subscription(session, tid)
            sub.plan_id = plan_id
            sub.status = "active"
            if extend_days:
                base = sub.period_end if sub.period_end and sub.period_end > utcnow() else utcnow()
                sub.period_end = base + timedelta(days=extend_days)
                sub.trial_ends_at = None
            session.add(
                AuditLog(
                    tenant_id=tid,
                    actor="admin:master",
                    action="billing.set_plan",
                    input={"plan_id": plan_id, "extend_days": extend_days},
                )
            )
            await session.commit()
    return RedirectResponse("/admin/tenants?flash=Plan actualizado", status_code=303)


@router.get("/tenants/nuevo", response_class=HTMLResponse)
async def tenants_new(request: Request):
    ctx = await _base_context(request, "tenants")
    if ctx is None:
        return _login_redirect()
    if not await _require_master(request):
        return RedirectResponse("/admin?flash=requiere_admin_master&flash_type=danger", status_code=303)
    ctx["available_packs"] = sorted({m.id for m in plugin_manager.list_modules()} - {CORE_PACK_ID})
    return _render("tenants_new.html", ctx)


@router.post("/tenants/nuevo")
async def tenants_create(
    request: Request,
    name: str = Form(default=""),
    slug: str = Form(default=""),
    business: str = Form(default=""),
    language: str = Form(default="es"),
    tone: str = Form(default="cercano"),
    packs: list[str] = Form(default=[]),  # noqa: B008
    escalation_triggers: str = Form(default=""),
    connect_whatsapp: bool = Form(default=False),
    wa_app_id: str = Form(default=""),
    wa_app_secret: str = Form(default=""),
    wa_waba_id: str = Form(default=""),
    wa_phone_number_id: str = Form(default=""),
    wa_access_token: str = Form(default=""),
    wa_verify_token: str = Form(default=""),
    admin_email: str = Form(default=""),
    admin_password: str = Form(default=""),
):
    if not await _require_master(request):
        return RedirectResponse("/admin?flash=requiere_admin_master&flash_type=danger", status_code=303)
    admin = await current_admin(request)
    if admin is None:
        return _login_redirect()

    tenant_slug = slug or _slugify(name)
    if not name or not tenant_slug:
        return RedirectResponse(
            "/admin/tenants/nuevo?flash=nombre y slug son obligatorios&flash_type=danger",
            status_code=303,
        )
    admin_email = admin_email.strip().lower()
    if admin_email and len(admin_password) < 8:
        return RedirectResponse(
            "/admin/tenants/nuevo?flash=La contraseña del administrador necesita 8+ caracteres"
            "&flash_type=danger",
            status_code=303,
        )

    triggers = [t.strip() for t in escalation_triggers.split(",") if t.strip()]
    try:
        from core.onboarding import onboard_tenant

        tid, _steps = await onboard_tenant(
            name=name,
            slug=tenant_slug,
            business_description=business,
            language=language,
            persona={"role": "asistente", "tone": tone},
            escalation_triggers=triggers or None,
            packs=packs or None,
            admin_email=admin_email or admin.email,
            admin_password=admin_password or None,
            whatsapp=None,
        )
        # El master que crea el tenant siempre queda vinculado (si el admin del
        # tenant es otro email), para no perderlo del selector de tenant.
        if admin_email and admin_email != admin.email:
            await _ensure_tenant_link(admin.id, tid, "owner")
    except Exception as exc:
        log.exception("tenant_create_failed")
        return RedirectResponse(
            f"/admin/tenants/nuevo?flash=Error al crear: {str(exc)[:140]}&flash_type=danger", status_code=303
        )

    # WhatsApp opcional vía el flujo BYOK (MetaConnection + suscripción app/WABA),
    # el mismo que /admin/conexiones: valida credenciales, registra la suscripción
    # y maneja la expiración del token. Si falla, el tenant YA quedó creado: se
    # avisa y se redirige a Conexiones para reintentar (no se aborta el alta).
    wa_error: str | None = None
    if connect_whatsapp:
        from core.gateway.meta_byok import ByokError, save_byok_connection

        try:
            async with tenant_scope(tid):
                await save_byok_connection(
                    tid,
                    admin.id,
                    wa_app_id.strip(),
                    wa_app_secret.strip(),
                    wa_phone_number_id.strip(),
                    wa_access_token.strip(),
                    wa_waba_id.strip(),
                    environment="sandbox",
                    verify_token=wa_verify_token.strip() or None,
                )
        except ByokError as exc:
            wa_error = str(exc)
        except Exception as exc:  # red/infra no clasificada
            log.exception("tenant_create_whatsapp_failed")
            wa_error = str(exc)[:180]

    if wa_error:
        response = RedirectResponse(
            f"/admin/conexiones?flash=Tenant {tenant_slug} creado, pero WhatsApp falló: "
            f"{wa_error[:200]}&flash_type=danger",
            status_code=303,
        )
    else:
        extra = " con WhatsApp" if connect_whatsapp else ""
        response = RedirectResponse(
            f"/admin/spec?flash=tenant {tenant_slug} creado{extra}&flash_type=success", status_code=303
        )
    response.set_cookie(
        TENANT_COOKIE,
        str(tid),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        secure=get_settings().app_env == "production",
    )
    return response


# --------------------------------------------------- ciclo de vida del tenant
async def _toggle_tenant_status(
    request: Request, tenant_id: uuid.UUID, status: str
) -> RedirectResponse:
    if not await _require_master(request):
        return RedirectResponse("/admin?flash=requiere_admin_master&flash_type=danger", status_code=303)
    from core.tenants import activate_tenant, deactivate_tenant

    ref = str(tenant_id)
    if status == "suspended":
        await deactivate_tenant(ref)
        flash = "tenant_suspendido"
    else:
        await activate_tenant(ref)
        flash = "tenant_activado"
    return RedirectResponse(f"/admin/tenants?flash={flash}&flash_type=success", status_code=303)


@router.post("/tenants/{tenant_id}/deactivate")
async def tenants_deactivate(request: Request, tenant_id: uuid.UUID):
    return await _toggle_tenant_status(request, tenant_id, "suspended")


@router.post("/tenants/{tenant_id}/activate")
async def tenants_activate(request: Request, tenant_id: uuid.UUID):
    return await _toggle_tenant_status(request, tenant_id, "active")


@router.post("/tenants/{tenant_id}/purge")
async def tenants_purge(request: Request, tenant_id: uuid.UUID, confirm_phrase: str = Form(default="")):
    """Eliminación física IRREVERSIBLE (excepción a la invariante #8).

    Exige que `confirm_phrase` sea exactamente "ELIMINAR <slug>". Solo master.
    """
    if not await _require_master(request):
        return RedirectResponse("/admin?flash=requiere_admin_master&flash_type=danger", status_code=303)
    from core.tenants import purge_tenant

    ref = str(tenant_id)
    try:
        await purge_tenant(ref, confirm_phrase)
    except ValueError as exc:
        return RedirectResponse(
            f"/admin/tenants?flash={str(exc)[:140]}&flash_type=danger", status_code=303
        )
    except Exception as exc:
        log.exception("tenant_purge_failed")
        return RedirectResponse(
            f"/admin/tenants?flash=Error al eliminar: {str(exc)[:140]}&flash_type=danger",
            status_code=303,
        )
    return RedirectResponse("/admin/tenants?flash=tenant_eliminado&flash_type=success", status_code=303)


# ------------------------------------------------------------------ colaboradores (ADR-0048)
async def _tenant_role(admin_id: uuid.UUID, tenant_id: uuid.UUID | None) -> str | None:
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


async def _require_collaborators(request: Request, *, write: bool = False) -> bool:
    """Gestión de cuentas: master (todos los tenants) u `owner` del tenant activo.

    El `colaborador` no administra cuentas. Un no-master nunca es master de
    plataforma (no ve otros tenants).
    """
    admin = await current_admin(request)
    if admin is None:
        return False
    if admin.role == "master":
        return True
    return await _tenant_role(admin.id, get_current_tenant_id()) == "owner"


def _collab_redirect(flash: str, flash_type: str = "success") -> RedirectResponse:
    return RedirectResponse(
        f"/admin/colaboradores?flash={flash}&flash_type={flash_type}", status_code=303
    )


def _audit(action: str, actor: str, target: str, **detail: Any) -> None:
    """Rastro de cambios de cuentas/RBAC (el AuditLog de negocio exige tenant_id)."""
    log.info("admin_rbac_change", action=action, actor=actor, target=target, **detail)


async def _resolve_tenant_id(slug: str) -> uuid.UUID | None:
    async with get_session() as session:
        return (
            await session.execute(select(Tenant.id).where(Tenant.slug == slug))
        ).scalar_one_or_none()


async def _ensure_tenant_link(admin_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> None:
    """Vincula un admin a un tenant si no lo estaba (idempotente)."""
    async with get_session() as session:
        existing = (
            await session.execute(
                select(TenantUser).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))


async def _collaborators_context(request: Request) -> dict[str, Any] | None:
    ctx = await _base_context(request, "collaborators")
    if ctx is None:
        return None
    me = await current_admin(request)
    is_master = me is not None and me.role == "master"
    tid = get_current_tenant_id()

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
                ).scalars().all()
            )
        admin_ids = [a.id for a in admins]
        perms = []
        links = []
        if admin_ids:
            perm_where = [AdminPermission.admin_id.in_(admin_ids)]
            link_where = [TenantUser.admin_id.in_(admin_ids)]
            if not is_master:
                # Un owner solo ve el scope de SU tenant, no la estructura
                # multi-tenant de cuentas compartidas.
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
        tenants = (
            await session.execute(
                select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.name)
            )
        ).scalars().all()

    perms_by_admin: dict[uuid.UUID, list[dict[str, str]]] = {}
    for admin_id, section, access, slug in perms:
        perms_by_admin.setdefault(admin_id, []).append(
            {"section": section, "access": access, "scope": slug or "*"}
        )
    links_by_admin: dict[uuid.UUID, list[dict[str, str]]] = {}
    for admin_id, role, slug, name in links:
        links_by_admin.setdefault(admin_id, []).append({"role": role, "slug": slug, "name": name})

    ctx["is_master"] = is_master
    ctx["me_id"] = str(me.id) if me else ""
    ctx["admins_rows"] = [
        {
            "id": str(a.id),
            "email": a.email,
            "role": a.role,
            "active": a.active,
            "is_self": me is not None and a.id == me.id,
            "is_platform_master": a.role == "master",
            "perms": sorted(
                perms_by_admin.get(a.id, []), key=lambda p: (p["section"], p["scope"])
            ),
            "links": links_by_admin.get(a.id, []),
        }
        for a in admins
    ]
    full = sections_catalog(plugin_manager.active_ids())
    if is_master:
        ctx["sections"] = full
        ctx["tenants_options"] = [{"slug": t.slug, "name": t.name} for t in tenants]
        ctx["active_tenant"] = None
    else:
        ctx["sections"] = {k: v for k, v in full.items() if k not in OWNER_GRANTABLE_SECTIONS}
        ctx["tenants_options"] = []
        ctx["active_tenant"] = str(tid) if tid else None
    return ctx


@router.get("/colaboradores", response_class=HTMLResponse)
async def collaborators_list(request: Request):
    ctx = await _collaborators_context(request)
    if ctx is None:
        return _login_redirect()
    if not await _require_collaborators(request):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    return _render("colaboradores.html", ctx)


@router.get("/admins")
async def admins_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/colaboradores", status_code=307)


@router.post("/colaboradores")
async def collaborators_create(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(default="colaborador"),
    username: str = Form(default=""),
    tenant: str = Form(default=""),
):
    if await current_admin(request) is None:
        return _login_redirect()
    me = await current_admin(request)
    if not await _require_collaborators(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    is_master = me is not None and me.role == "master"

    email = email.strip().lower()
    if not email or not password:
        return _collab_redirect("email y contraseña son obligatorios", "danger")
    if role not in ("master", "owner", "colaborador"):
        return _collab_redirect("rol inválido", "danger")
    if role == "master" and not is_master:
        return _collab_redirect("no podés crear un master de plataforma", "danger")

    if is_master:
        admin_role = "master" if role == "master" else "colaborador"
        tenant_id = await _resolve_tenant_id(tenant) if tenant else None
        if tenant and tenant_id is None:
            return _collab_redirect("tenant no encontrado", "danger")
        tenant_role = "colaborador" if role == "colaborador" else "owner"
    else:
        admin_role = "colaborador"
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return _collab_redirect("no hay tenant activo", "danger")
        tenant_role = "owner" if role == "owner" else "colaborador"

    async with get_session() as session:
        existing = (
            await session.execute(select(Admin).where(Admin.email == email))
        ).scalar_one_or_none()
        if existing is not None:
            return _collab_redirect(f"el admin {email} ya existe", "danger")
        new_admin = Admin(
            email=email,
            username=username.strip() or None,
            password_hash=hash_password(password),
            role=admin_role,
        )
        session.add(new_admin)
        await session.flush()
        if tenant_id is not None:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=new_admin.id, role=tenant_role))
    _audit(
        "create_collaborator",
        me.email if me else "?",
        email,
        role=admin_role,
        tenant=tenant or "*",
    )
    return _collab_redirect(f"colaborador {email} creado")


async def _target_allowed_for_owner(admin_id: uuid.UUID, tid: uuid.UUID | None) -> bool:
    """Un owner solo gestiona cuentas EXCLUSIVAS de su tenant.

    No puede tocar a un master de plataforma, ni a una cuenta con permisos
    globales o vinculada a otro tenant (compartida): esas las administra el
    master. Si no, podría resetear su contraseña y secuestrar la cuenta.
    """
    if tid is None:
        return False
    async with get_session() as session:
        target = await session.get(Admin, admin_id)
        if target is None or target.role == "master":
            return False
        link = (
            await session.execute(
                select(TenantUser.role).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tid
                )
            )
        ).scalar_one_or_none()
        if link is None:
            return False
        global_perms = (
            await session.execute(
                select(func.count())
                .select_from(AdminPermission)
                .where(
                    AdminPermission.admin_id == admin_id,
                    AdminPermission.tenant_id.is_(None),
                )
            )
        ).scalar_one()
        other_links = (
            await session.execute(
                select(func.count())
                .select_from(TenantUser)
                .where(TenantUser.admin_id == admin_id, TenantUser.tenant_id != tid)
            )
        ).scalar_one()
    return global_perms == 0 and other_links == 0


@router.post("/colaboradores/{admin_id}/password")
async def collaborators_password(
    request: Request, admin_id: uuid.UUID, password: str = Form(...)
):
    if await current_admin(request) is None:
        return _login_redirect()
    me = await current_admin(request)
    if not await _require_collaborators(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    is_master = me is not None and me.role == "master"
    if not password:
        return _collab_redirect("la contraseña no puede estar vacía", "danger")
    if not is_master and not await _target_allowed_for_owner(admin_id, get_current_tenant_id()):
        return _collab_redirect("no podés cambiar esa contraseña", "danger")
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin is None:
            return _collab_redirect("admin no encontrado", "danger")
        admin.password_hash = hash_password(password)
    _audit("reset_password", me.email if me else "?", str(admin_id))
    return _collab_redirect("contraseña actualizada")


@router.post("/colaboradores/{admin_id}/grant")
async def collaborators_grant(
    request: Request,
    admin_id: uuid.UUID,
    section: str = Form(...),
    access: str = Form(default="read"),
    tenant: str = Form(default=""),
):
    if await current_admin(request) is None:
        return _login_redirect()
    me = await current_admin(request)
    if not await _require_collaborators(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    is_master = me is not None and me.role == "master"
    if section not in sections_catalog(plugin_manager.active_ids()):
        return _collab_redirect("sección inválida", "danger")
    if access not in ("read", "write"):
        return _collab_redirect("acceso inválido", "danger")

    if is_master:
        tenant_id = await _resolve_tenant_id(tenant) if tenant else None
        if tenant and tenant_id is None:
            return _collab_redirect("tenant no encontrado", "danger")
    else:
        if section in OWNER_GRANTABLE_SECTIONS:
            return _collab_redirect("no podés otorgar esa sección", "danger")
        if not await _target_allowed_for_owner(admin_id, get_current_tenant_id()):
            return _collab_redirect("no podés otorgar permisos a esa cuenta", "danger")
        tenant_id = get_current_tenant_id()

    async with get_session() as session:
        row = (
            await session.execute(
                select(AdminPermission).where(
                    AdminPermission.admin_id == admin_id,
                    AdminPermission.section == section,
                    AdminPermission.tenant_id.is_(None)
                    if tenant_id is None
                    else AdminPermission.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                AdminPermission(
                    admin_id=admin_id, section=section, access=access, tenant_id=tenant_id
                )
            )
        else:
            row.access = access
    _audit(
        "grant", me.email if me else "?", str(admin_id),
        section=section, access=access, scope=tenant or "*",
    )
    return _collab_redirect(f"permiso {section} → {access} concedido")


@router.post("/colaboradores/{admin_id}/revoke")
async def collaborators_revoke(
    request: Request,
    admin_id: uuid.UUID,
    section: str = Form(...),
    tenant: str = Form(default=""),
):
    if await current_admin(request) is None:
        return _login_redirect()
    me = await current_admin(request)
    if not await _require_collaborators(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    is_master = me is not None and me.role == "master"
    if section not in sections_catalog(plugin_manager.active_ids()):
        return _collab_redirect("sección inválida", "danger")
    if is_master:
        tenant_id = await _resolve_tenant_id(tenant) if tenant else None
    else:
        if not await _target_allowed_for_owner(admin_id, get_current_tenant_id()):
            return _collab_redirect("no podés revocar permisos de esa cuenta", "danger")
        tenant_id = get_current_tenant_id()
    async with get_session() as session:
        await session.execute(
            delete(AdminPermission).where(
                AdminPermission.admin_id == admin_id,
                AdminPermission.section == section,
                AdminPermission.tenant_id.is_(None)
                if tenant_id is None
                else AdminPermission.tenant_id == tenant_id,
            )
        )
    _audit("revoke", me.email if me else "?", str(admin_id), section=section, scope=tenant or "*")
    return _collab_redirect(f"permiso {section} revocado")


@router.post("/colaboradores/{admin_id}/link")
async def collaborators_link(
    request: Request,
    admin_id: uuid.UUID,
    tenant: str = Form(...),
    role: str = Form(default="colaborador"),
):
    """Vincular un admin existente a un tenant: solo master."""
    me = await current_admin(request)
    if me is None:
        return _login_redirect()
    if me.role != "master":
        return _collab_redirect("solo un master puede vincular admins", "danger")
    if role not in ("owner", "colaborador"):
        return _collab_redirect("rol inválido", "danger")
    tenant_id = await _resolve_tenant_id(tenant)
    if tenant_id is None:
        return _collab_redirect("tenant no encontrado", "danger")
    async with get_session() as session:
        row = (
            await session.execute(
                select(TenantUser).where(
                    TenantUser.admin_id == admin_id, TenantUser.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))
        else:
            row.role = role
    _audit("link", me.email, str(admin_id), tenant=tenant, role=role)
    return _collab_redirect(f"vinculado a {tenant} ({role})")


@router.post("/colaboradores/{admin_id}/toggle")
async def collaborators_toggle(request: Request, admin_id: uuid.UUID):
    if await current_admin(request) is None:
        return _login_redirect()
    me = await current_admin(request)
    if not await _require_collaborators(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    is_master = me is not None and me.role == "master"
    if me is not None and me.id == admin_id:
        return _collab_redirect("no podés desactivar tu propio usuario", "danger")
    if not is_master and not await _target_allowed_for_owner(admin_id, get_current_tenant_id()):
        return _collab_redirect("no podés modificar esa cuenta", "danger")
    async with get_session() as session:
        admin = await session.get(Admin, admin_id)
        if admin is None:
            return _collab_redirect("admin no encontrado", "danger")
        admin.active = not admin.active
        active = admin.active
        state = "activado" if admin.active else "desactivado"
    _audit("toggle", me.email if me else "?", str(admin_id), active=active)
    return _collab_redirect(f"admin {state}")


# ------------------------------------------------------------------ AgentSpec
async def _spec_versions() -> list[dict[str, Any]]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(AgentSpecRecord)
                .where(
                    AgentSpecRecord.tenant_id == get_current_tenant_id(),
                    AgentSpecRecord.deleted_at.is_(None),
                )
                .order_by(AgentSpecRecord.created_at.desc())
            )
        ).scalars().all()
    return [
        {"version": r.version, "is_active": r.is_active, "created_at": r.created_at}
        for r in rows
    ]


async def _next_spec_version() -> str:
    """Versión nueva: sube la patch de la versión más alta existente (1.0.0 → 1.0.1)."""
    versions = await _spec_versions()
    nums: list[list[int]] = []
    for v in versions:
        parts = [int(x) for x in str(v["version"]).split(".") if x.isdigit()]
        if parts:
            nums.append(parts)
    if not nums:
        return "1.0.0"
    base = max(nums, key=lambda p: tuple(([*p, 0, 0])[:3]))
    base = ([*base, 0, 0])[:3]
    base[2] += 1
    return ".".join(str(x) for x in base)


@router.get("/spec", response_class=HTMLResponse)
async def spec_editor(request: Request) -> HTMLResponse:
    """Editor del AgentSpec del tenant activo (C4, Sprint 4). Form por secciones
    + vista JSON cruda validada (fallback de poder). Guardar crea una versión
    nueva y la activa (misma semántica que `agent-core agent activate`)."""
    ctx = await _base_context(request, "spec")
    if ctx is None:
        return _login_redirect()
    spec = await load_active_spec()
    ctx["spec"] = spec.model_dump(mode="json")
    ctx["versions"] = await _spec_versions()
    return _render("spec.html", ctx)


async def _ensure_pack_activated(pack: str) -> str | None:
    """Activa un pack si su código todavía no estaba cargado. Devuelve el error
    (str) o None si quedó activo (o ya lo estaba).

    Un pack con config obligatoria y sin fila previa para el tenant actual (ej.
    nunca pasó por `tenant create --packs` ni por `/admin/plugins/<id>/configurar`)
    no activa: `load_config` exige una fila existente. Se intenta una config
    vacía por default (mismo patrón que ya usa `onboard_tenant` en
    core/onboarding.py) para los packs cuyo schema no tiene campos obligatorios.

    Si el schema SÍ exige campos reales (ej. credenciales de una pasarela de
    pago), antes esto dejaba pasar el `ValidationError` crudo de pydantic tal
    cual al flash del admin ("3 validation errors for PaymentsConfig...") --
    inentendible para quien solo apretó el botón "Activar" en vez de
    "Configurar" primero. Ahora se traduce a un mensaje accionable, sin pisar
    una configuración real si ya existía.
    """
    if plugin_manager.is_active(pack):
        return None
    try:
        module = plugin_manager.get(pack)
        if module is not None and module.config_schema is not None:
            async with get_session() as session:
                has_config = (
                    await session.execute(
                        select(PluginConfig).where(
                            PluginConfig.plugin_id == pack,
                            PluginConfig.tenant_id == get_current_tenant_id(),
                        )
                    )
                ).scalar_one_or_none() is not None
            if not has_config:
                try:
                    await plugin_manager.save_config(pack, {})
                except PluginConfigError:
                    return f'{pack} necesita configuración -- usá el botón "Configurar" (⚙️) primero'
        await plugin_manager.activate(pack)
        return None
    except PluginError as exc:
        return f"{pack}: {exc}"


async def _save_spec_version(new_spec: AgentSpec) -> int | None:
    """Persiste `new_spec` como nueva versión activa del tenant actual.

    Desactiva la versión previa e inserta la nueva en la misma transacción.
    Devuelve la versión nueva, o None si hubo una edición concurrente (los
    índices parciales protegen la integridad; el llamador decide cómo avisar).
    """
    version = await _next_spec_version()
    try:
        async with get_session() as session:
            current_row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == get_current_tenant_id(),
                        AgentSpecRecord.is_active.is_(True),
                        AgentSpecRecord.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if current_row is not None:
                current_row.is_active = False
            session.add(
                AgentSpecRecord(
                    tenant_id=get_current_tenant_id(),
                    version=version,
                    is_active=True,
                    spec=new_spec.model_dump(mode="json"),
                )
            )
    except IntegrityError:
        return None
    return version


@router.post("/spec")
async def spec_save(request: Request):
    admin = await current_admin(request)
    if admin is None:
        return _login_redirect()
    form = await request.form()
    persona_role = str(form.get("persona_role", ""))
    persona_tone = str(form.get("persona_tone", ""))
    try:
        persona_warmth = int(form.get("persona_warmth", 3) or 3)
    except ValueError:
        persona_warmth = 3
    try:
        persona_max_length = int(form.get("persona_max_length", 500) or 500)
    except ValueError:
        persona_max_length = 500
    persona_forbidden = str(form.get("persona_forbidden", ""))
    persona_emojis = str(form.get("persona_emojis", "sparse"))
    model_primary = str(form.get("model_primary", ""))
    try:
        model_temperature = float(form.get("model_temperature", 0.3) or 0.3)
    except ValueError:
        model_temperature = 0.3
    try:
        policy_budget = float(form.get("policy_budget", 0.5) or 0.5)
    except ValueError:
        policy_budget = 0.5
    try:
        policy_max_tool_calls = int(form.get("policy_max_tool_calls", 10) or 10)
    except ValueError:
        policy_max_tool_calls = 10
    raw_json = str(form.get("raw_json", ""))

    current = await load_active_spec()
    try:
        if raw_json.strip():
            # fallback de poder: JSON completo validado por el contrato congelado
            data = json.loads(raw_json)
            new_spec = AgentSpec.model_validate(data)
        else:
            # Los packs ya no se editan acá: se activan/desactivan por tenant
            # desde /admin/plugins (una sola pantalla, sin redundancia).
            data = current.model_dump(mode="json")
            data["persona"].update(
                {
                    "role": persona_role or data["persona"].get("role", ""),
                    "tone": persona_tone or data["persona"].get("tone", ""),
                    "warmth": persona_warmth,
                    "emojis": persona_emojis or data["persona"].get("emojis", "sparse"),
                    "maxLength": persona_max_length,
                    "forbiddenPhrases": [
                        p.strip() for p in persona_forbidden.split(",") if p.strip()
                    ],
                }
            )
            if model_primary:
                data["model"]["primary"] = model_primary
            data["model"]["temperature"] = model_temperature
            data["policies"]["budgetUsdPerConversation"] = policy_budget
            data["policies"]["maxToolCallsPerTurn"] = policy_max_tool_calls
            # `policies.escalation.triggers` NO se toca acá -- ese campo del
            # contrato nunca se lee en runtime (ver `AppSettings.escalation_config`
            # en /admin/configuracion, que sí es lo que usa el orchestrator).
            new_spec = AgentSpec.model_validate(data)
    except Exception as exc:
        return RedirectResponse(
            f"/admin/spec?flash=spec_invalido: {exc}&flash_type=danger", status_code=303
        )

    # `raw_json` es el único camino que puede seguir tocando `packs` (fallback
    # de poder); si agrega alguno nuevo, se activa igual que desde Plugins.
    activation_errors = [
        err
        for pack in set(new_spec.packs) - set(current.packs)
        if (err := await _ensure_pack_activated(pack)) is not None
    ]

    version = await _save_spec_version(new_spec)
    if version is None:
        return RedirectResponse(
            "/admin/spec?flash=hubo una edición concurrente, reintenta&flash_type=danger",
            status_code=303,
        )
    if activation_errors:
        return RedirectResponse(
            f"/admin/spec?flash=spec guardado (v{version}), pero no se pudo activar: "
            f"{'; '.join(activation_errors)}&flash_type=danger",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/spec?flash=spec guardado y activado (v{version})&flash_type=success",
        status_code=303,
    )


# ------------------------------------------------------------------ dashboard
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ctx = await _base_context(request, "dashboard")
    if ctx is None:
        return _login_redirect()

    week_ago = datetime.now(UTC) - timedelta(days=7)
    month_ago = datetime.now(UTC) - timedelta(days=30)
    my_tenant = get_current_tenant_id()

    async with get_session() as session:
        conversations_7d = (
            await session.execute(
                select(func.count(Conversation.id)).where(
                    Conversation.tenant_id == my_tenant,
                    Conversation.started_at >= week_ago,
                )
            )
        ).scalar() or 0
        messages_7d = (
            await session.execute(
                select(func.count(Message.id)).where(
                    Message.tenant_id == my_tenant,
                    Message.created_at >= week_ago,
                )
            )
        ).scalar() or 0
        pending = (
            await session.execute(
                select(func.count(Escalation.id)).where(
                    Escalation.tenant_id == my_tenant,
                    Escalation.status == "pendiente",
                )
            )
        ).scalar() or 0
        cost_30d = (
            await session.execute(
                select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
                    UsageLedger.tenant_id == my_tenant,
                    UsageLedger.created_at >= month_ago,
                )
            )
        ).scalar() or 0
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == my_tenant,
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        recent_rows = (
            await session.execute(
                select(Conversation, Customer)
                .join(Customer, Customer.id == Conversation.customer_id)
                .where(Conversation.tenant_id == my_tenant)
                .order_by(Conversation.started_at.desc())
                .limit(8)
            )
        ).all()

    settings = get_settings()
    llm_provider = (app_settings.llm_provider if app_settings else None) or settings.llm_provider
    llm_model = (app_settings.llm_model if app_settings else None) or settings.llm_model

    ctx.update(
        kpi={
            "conversations_7d": conversations_7d,
            "messages_7d": messages_7d,
            "pending_escalations": pending,
            "cost_30d": float(cost_30d),
            "active_plugins": len([p for p in plugin_manager.list_modules()
                                   if plugin_manager.is_active(p.id)]),
        },
        bot_paused=app_settings.bot_paused if app_settings else False,
        llm_label=f"{llm_provider} · {llm_model}",
        recent=[
            {
                "id": conv.id,
                "customer_name": cust.name,
                "external_id": cust.external_id,
                "status": conv.status,
            }
            for conv, cust in recent_rows
        ],
    )
    return _render("dashboard.html", ctx)


# ------------------------------------------------------------------ conversaciones
@router.get("/conversaciones", response_class=HTMLResponse)
async def conversations_list(request: Request):
    ctx = await _base_context(request, "conversations")
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Conversation, Customer)
                .join(Customer, Customer.id == Conversation.customer_id)
                .order_by(Conversation.started_at.desc())
                .limit(100)
            )
        ).all()
    ctx["conversations"] = [
        {
            "id": conv.id,
            "customer_name": cust.name,
            "external_id": cust.external_id,
            "channel": conv.channel,
            "status": conv.status,
            "bot_paused": conv.bot_paused,
            "started_at": conv.started_at,
        }
        for conv, cust in rows
    ]
    return _render("conversations.html", ctx)


@router.get("/conversaciones/{conversation_id}", response_class=HTMLResponse)
async def conversation_detail(request: Request, conversation_id: uuid.UUID):
    ctx = await _base_context(request, "conversations")
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return RedirectResponse("/admin/conversaciones", status_code=303)
        customer = await session.get(Customer, conversation.customer_id)
        messages = (
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )
    ctx.update(
        conversation_id=conversation.id,
        customer_name=customer.name if customer else None,
        external_id=customer.external_id if customer else "",
        status=conversation.status,
        bot_paused=conversation.bot_paused,
        channel=conversation.channel,
        messages=messages,
    )
    return _render("conversation_detail.html", ctx)


@router.post("/conversaciones/{conversation_id}/mensaje")
async def send_human_message(request: Request, conversation_id: uuid.UUID, content: str = Form(...)):
    if await current_admin(request) is None:
        return _login_redirect()
    from core.gateway.sender import send_to_conversation

    await send_to_conversation(conversation_id, content, sender_type="human")
    return RedirectResponse(f"/admin/conversaciones/{conversation_id}", status_code=303)


@router.post("/conversaciones/{conversation_id}/toggle-bot")
async def toggle_conversation_bot(request: Request, conversation_id: uuid.UUID):
    if await current_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.bot_paused = not conversation.bot_paused
    return RedirectResponse("/admin/conversaciones", status_code=303)


@router.post("/conversaciones/{conversation_id}/resolver")
async def resolve_conversation(request: Request, conversation_id: uuid.UUID):
    if await current_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.status = "open"
            conversation.bot_paused = False
        escalations = (
            (
                await session.execute(
                    select(Escalation).where(
                        Escalation.conversation_id == conversation_id,
                        Escalation.status != "resuelta",
                    )
                )
            )
            .scalars()
            .all()
        )
        for escalation in escalations:
            escalation.status = "resuelta"
            escalation.resolved_at = datetime.now(UTC)
    return RedirectResponse(f"/admin/conversaciones/{conversation_id}?flash=Conversación resuelta", status_code=303)


# ------------------------------------------------------------------ escalaciones
@router.get("/escalaciones", response_class=HTMLResponse)
async def escalations_list(request: Request):
    ctx = await _base_context(request, "escalations")
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Escalation, Conversation, Customer)
                .join(Conversation, Conversation.id == Escalation.conversation_id)
                .join(Customer, Customer.id == Conversation.customer_id)
                .order_by(Escalation.created_at.desc())
                .limit(100)
            )
        ).all()
    ctx["escalations"] = [
        {
            "conversation_id": conv.id,
            "customer_name": cust.name,
            "external_id": cust.external_id,
            "reason": esc.reason,
            "summary": esc.summary,
            "status": esc.status,
            "created_at": esc.created_at,
        }
        for esc, conv, cust in rows
    ]
    return _render("escalations.html", ctx)


# ------------------------------------------------------------------ plugins
@router.get("/plugins", response_class=HTMLResponse)
async def plugins_list(request: Request):
    """Packs disponibles y si están habilitados para el TENANT ACTUAL.

    Activar/desactivar acá edita `AgentSpec.packs` de este tenant únicamente
    (nueva versión del spec, igual que antes hacía el formulario de AgentSpec —
    esa sección se eliminó de ahí por redundante). El estado "instalado"/"error"
    es informativo y de sistema (¿está el código cargado en el servidor?); no
    determina el botón, que refleja el consentimiento de este tenant.
    """
    ctx = await _base_context(request, "plugins")
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        records = {r.id: r for r in (await session.execute(select(PluginRecord))).scalars().all()}
    spec = await load_active_spec()
    enabled = enabled_packs(spec)
    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        tenant_state = {
            r.plugin_id: r
            for r in (
                await session.execute(
                    select(PluginTenantState).where(PluginTenantState.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        }

    plugins = []
    for module in plugin_manager.list_modules():
        if module.id == CORE_PACK_ID:
            continue
        record = records.get(module.id)
        health = tenant_state.get(module.id)
        plugins.append(
            {
                "id": module.id,
                "name": module.name or module.id,
                "version": module.version,
                "status": record.status if record else "detected",
                "error_message": record.error_message if record else None,
                "has_config": module.config_schema is not None,
                "enabled_for_tenant": module.id in enabled,
                "tenant_health_error": health.error_message if health else None,
            }
        )
    ctx["plugins"] = plugins
    return _render("plugins.html", ctx)


@router.get("/plugins/{plugin_id}/configurar", response_class=HTMLResponse)
async def plugin_config_page(request: Request, plugin_id: str):
    ctx = await _base_context(request, "plugins")
    if ctx is None:
        return _login_redirect()
    module = plugin_manager.get(plugin_id)
    if module is None or module.config_schema is None:
        return RedirectResponse("/admin/plugins", status_code=303)

    current_values: dict[str, Any] = {}
    try:
        config = await plugin_manager.load_config(plugin_id)
        if config is not None:
            current_values = config.model_dump()
    except Exception:
        pass

    ctx.update(
        plugin_id=plugin_id,
        plugin_name=module.name or plugin_id,
        plugin_description=module.description,
        form_fields=render_form_fields(module.config_schema, current_values),
    )
    return _render("plugin_config.html", ctx)


@router.post("/plugins/{plugin_id}/configurar")
async def plugin_config_save(request: Request, plugin_id: str):
    if await current_admin(request) is None:
        return _login_redirect()
    module = plugin_manager.get(plugin_id)
    if module is None or module.config_schema is None:
        return RedirectResponse("/admin/plugins", status_code=303)

    form = await request.form()
    form_data = {str(k): str(v) for k, v in form.items()}

    previous: dict[str, Any] = {}
    try:
        async with get_session() as session:
            stored = (
                await session.execute(
                    select(PluginConfig).where(
                        PluginConfig.plugin_id == plugin_id,
                        PluginConfig.tenant_id == get_current_tenant_id(),
                    )
                )
            ).scalar_one_or_none()
        if stored is not None:
            previous = get_secret_box().decrypt_json(stored.config_encrypted)
    except Exception:
        pass

    try:
        values = parse_form_values(module.config_schema, form_data, previous)
        await plugin_manager.save_config(plugin_id, values)
        await plugin_manager.activate(plugin_id)
    except Exception as exc:
        log.exception("plugin_config_save_failed", plugin=plugin_id)
        return RedirectResponse(
            f"/admin/plugins?flash=Error: {str(exc)[:120]}&flash_type=danger", status_code=303
        )
    # Configurarlo implica que este tenant lo quiere usar.
    try:
        await _enable_pack_for_tenant(plugin_id)
    except QuotaExceeded as exc:
        return _quota_redirect(exc)
    return RedirectResponse("/admin/plugins?flash=Plugin configurado y activado", status_code=303)


def _quota_redirect(exc: QuotaExceeded) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/plugins?flash=Tu plan no permite habilitar más plugins "
        f"({exc.used}/{exc.limit}). Mejorá tu plan en /admin/plan.&flash_type=danger",
        status_code=303,
    )


async def _enable_pack_for_tenant(pack: str) -> int | None:
    """Agrega `pack` a `AgentSpec.packs` del tenant actual (nueva versión).

    Aplica la cuota de plugins del plan (ADR-0045): si ya está en el límite y el
    pack no estaba habilitado, levanta `QuotaExceeded`.
    """
    spec = await load_active_spec()
    if pack in spec.packs:
        return None
    tenant_id = get_current_tenant_id()
    if tenant_id is not None and not await can_activate_plugin(tenant_id):
        raise QuotaExceeded("plugins", len(spec.packs), len(spec.packs))
    new_spec = spec.model_copy(update={"packs": sorted({*spec.packs, pack})})
    return await _save_spec_version(new_spec)


async def _disable_pack_for_tenant(pack: str) -> int | None:
    """Quita `pack` de `AgentSpec.packs` del tenant actual (nueva versión).

    NO desactiva el código globalmente — otro tenant puede seguir usándolo.
    """
    spec = await load_active_spec()
    if pack not in spec.packs:
        return None
    new_spec = spec.model_copy(update={"packs": sorted(set(spec.packs) - {pack})})
    return await _save_spec_version(new_spec)


@router.post("/plugins/{plugin_id}/activar")
async def plugin_activate(request: Request, plugin_id: str):
    """Habilita `plugin_id` para el TENANT ACTUAL únicamente (AgentSpec.packs)."""
    if await current_admin(request) is None:
        return _login_redirect()
    error = await _ensure_pack_activated(plugin_id)
    if error is not None:
        return RedirectResponse(f"/admin/plugins?flash=Error: {error}&flash_type=danger", status_code=303)
    try:
        await _enable_pack_for_tenant(plugin_id)
    except QuotaExceeded as exc:
        return _quota_redirect(exc)
    return RedirectResponse("/admin/plugins?flash=Plugin activado para este tenant", status_code=303)


@router.post("/plugins/{plugin_id}/desactivar")
async def plugin_deactivate(request: Request, plugin_id: str):
    """Deshabilita `plugin_id` para el TENANT ACTUAL únicamente. El código sigue
    cargado en el servidor por si otro tenant lo usa."""
    if await current_admin(request) is None:
        return _login_redirect()
    await _disable_pack_for_tenant(plugin_id)
    return RedirectResponse("/admin/plugins?flash=Plugin desactivado para este tenant", status_code=303)


@router.post("/plugins/{plugin_id}/reintentar")
async def plugin_reintentar(request: Request, plugin_id: str):
    """Borra la salud de error de `plugin_id` para el TENANT ACTUAL (ADR-0024)
    -- para usar después de corregir la causa del error (o si fue un problema
    transitorio). No toca el estado de sistema ni a otros tenants."""
    if await current_admin(request) is None:
        return _login_redirect()
    await plugin_manager.reactivate_for_tenant(get_current_tenant_id(), plugin_id)
    return RedirectResponse("/admin/plugins?flash=Plugin reactivado para este tenant", status_code=303)


# ------------------------------------------------------------------ conexiones
@router.get("/conexiones", response_class=HTMLResponse)
async def connections_page(request: Request):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()
    connections = await list_connections()

    # URL del webhook por tenant (cada app tiene el suyo: /webhooks/whatsapp/<slug>).
    from core.db.models import PhoneNumber, Tenant
    from core.tenants import tenant_webhook_url

    tenant_slug = ""
    tid = get_current_tenant_id()
    if tid is not None:
        async with get_session() as session:
            tenant = await session.get(Tenant, tid)
            tenant_slug = tenant.slug if tenant else ""

    rows = [
        {
            "id": c.id,
            "tipo": "channel_connection",
            "origen": "Manual",
            "channel": c.channel,
            "label": c.label or c.external_id,
            "external_id": c.external_id,
            "active": c.active,
            "is_primary": c.is_primary,
            "created_at": c.created_at,
            "detail_url": None,
        }
        for c in connections
    ]

    settings = get_settings()
    if tid is not None:
        async with get_session() as session:
            meta_connections = (
                await session.execute(
                    select(MetaConnection).where(
                        MetaConnection.tenant_id == tid, MetaConnection.deleted_at.is_(None)
                    )
                )
            ).scalars().all()
            phones_by_connection: dict[uuid.UUID, str] = {}
            if meta_connections:
                phone_rows = (
                    await session.execute(
                        select(PhoneNumber).where(
                            PhoneNumber.tenant_id == tid,
                            PhoneNumber.connection_id.in_([mc.id for mc in meta_connections]),
                        )
                    )
                ).scalars().all()
                for p in phone_rows:
                    phones_by_connection.setdefault(p.connection_id, p.display_phone_number or p.phone_number_id)

        for mc in meta_connections:
            origen = "Embedded Signup" if mc.meta_app_id == settings.meta_app_id else "BYOK (app propia)"
            rows.append({
                "id": mc.id,
                "tipo": "meta_connection",
                "origen": origen,
                "channel": mc.channel,
                "label": f"{mc.channel} · {mc.environment}",
                "external_id": phones_by_connection.get(mc.id, mc.meta_app_id),
                "active": mc.status == "connected",
                "is_primary": False,
                "created_at": mc.created_at,
                "detail_url": "/admin/conexiones/salud",
            })

    rows.sort(key=lambda r: r["created_at"] or utcnow(), reverse=True)

    ctx.update(
        connections=rows,
        has_connection=bool(rows),
        webhook_url=tenant_webhook_url(tenant_slug) if tenant_slug else "",
    )
    return _render("conexiones.html", ctx)


@router.post("/conexiones/guardar")
async def connection_save(
    request: Request,
    connection_id: str = Form(default=""),
    label: str = Form(default=""),
    phone_number_id: str = Form(default=""),
    app_secret: str = Form(default=""),
    access_token: str = Form(default=""),
    verify_token: str = Form(default=""),
):
    if await current_admin(request) is None:
        return _login_redirect()

    connection_id_uuid = uuid.UUID(connection_id) if connection_id else None

    # Verificar contra Meta ANTES de persistir (ag-sale: una credencial mala
    # guardada rompe el canal y el síntoma aparece recién en un mensaje que
    # no llega).
    ok, msg, info = await verify_meta_credentials(phone_number_id, access_token, app_secret)
    log.info(
        "connection_verify",
        tenant_id=str(get_current_tenant_id() or ""),
        phone_number_id=phone_number_id,
        ok=ok,
        msg=msg[:200],
    )
    if not ok:
        return RedirectResponse(
            f"/admin/conexiones?flash={msg[:140]}&flash_type=danger", status_code=303
        )

    try:
        from core.gateway.connections import save_connection

        credentials = {
            "phone_number_id": phone_number_id,
            "app_secret": app_secret,
            "access_token": access_token,
            "verify_token": verify_token,
        }
        _connection, _created = await save_connection(label, credentials, connection_id_uuid)
        meta_name = info.get("verified_name") or info.get("display_phone_number") or ""
        log.info(
            "connection_saved",
            tenant_id=str(get_current_tenant_id() or ""),
            phone_number_id=phone_number_id,
            created=_created,
            connection_id=str(_connection.id),
        )
        flash = f"Conectado con Meta ✓ {meta_name}".strip()
        return RedirectResponse(
            f"/admin/conexiones?flash={flash}", status_code=303
        )
    except Exception as exc:
        log.exception("connection_save_failed")
        return RedirectResponse(
            f"/admin/conexiones?flash=Error al guardar: {str(exc)[:120]}&flash_type=danger",
            status_code=303,
        )


@router.post("/conexiones/{connection_id}/activar")
async def connection_activate(request: Request, connection_id: uuid.UUID):
    if await current_admin(request) is None:
        return _login_redirect()
    await set_active(connection_id, True)
    return RedirectResponse("/admin/conexiones?flash=Conexión activada", status_code=303)


@router.post("/conexiones/{connection_id}/desactivar")
async def connection_deactivate(request: Request, connection_id: uuid.UUID):
    if await current_admin(request) is None:
        return _login_redirect()
    await set_active(connection_id, False)
    return RedirectResponse("/admin/conexiones?flash=Conexión desactivada", status_code=303)


@router.post("/conexiones/{connection_id}/primaria")
async def connection_set_primary(request: Request, connection_id: uuid.UUID):
    if await current_admin(request) is None:
        return _login_redirect()
    await set_primary(connection_id)
    return RedirectResponse("/admin/conexiones?flash=Se marcó como conexión principal", status_code=303)


@router.post("/conexiones/{connection_id}/eliminar")
async def connection_delete(request: Request, connection_id: uuid.UUID):
    if await current_admin(request) is None:
        return _login_redirect()
    await delete_connection(connection_id)
    return RedirectResponse("/admin/conexiones?flash=Conexión eliminada", status_code=303)


# ------------------------------------------------------------------ meta requisitos (preflight)
@router.get("/conexiones/meta-requisitos", response_class=HTMLResponse)
async def meta_requisitos_page(request: Request):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    from core.db.models import MetaSetupChecklist, Tenant
    from core.gateway.meta_preflight import MANUAL_STEPS, resolve_app_credentials, run_preflight

    tid = get_current_tenant_id()
    tenant_slug = ""
    if tid is not None:
        async with get_session() as session:
            tenant = await session.get(Tenant, tid)
            tenant_slug = tenant.slug if tenant else ""

    config = await resolve_app_credentials(tid)
    checks = await run_preflight(tid, tenant_slug)

    async with get_session() as session:
        done_rows = (
            await session.execute(select(MetaSetupChecklist).where(MetaSetupChecklist.tenant_id == tid))
        ).scalars().all()
    done_by_key = {row.step_key: row for row in done_rows}

    app_dashboard_base = (
        f"https://developers.facebook.com/apps/{config.app_id}/" if config else "https://developers.facebook.com/apps/"
    )
    steps = [
        {
            "key": step["key"],
            "label": step["label"],
            "link": app_dashboard_base + step["link_suffix"] if config else "https://developers.facebook.com/apps/",
            "done": done_by_key[step["key"]].done if step["key"] in done_by_key else False,
            "notes": done_by_key[step["key"]].notes if step["key"] in done_by_key else "",
        }
        for step in MANUAL_STEPS
    ]

    ctx.update(steps=steps, checks=checks, meta_configured=config is not None)
    return _render("meta_requisitos.html", ctx)


@router.get("/conexiones/meta-requisitos.json")
async def meta_requisitos_json(request: Request):
    if await current_admin(request) is None:
        return _login_redirect()

    from core.db.models import Tenant
    from core.gateway.meta_preflight import run_preflight

    tid = get_current_tenant_id()
    tenant_slug = ""
    if tid is not None:
        async with get_session() as session:
            tenant = await session.get(Tenant, tid)
            tenant_slug = tenant.slug if tenant else ""

    checks = await run_preflight(tid, tenant_slug)
    return {"checks": [c.model_dump() for c in checks]}


@router.post("/conexiones/meta-requisitos/paso")
async def meta_requisitos_toggle(
    request: Request,
    step_key: str = Form(...),
    done: str = Form(default=""),
    notes: str = Form(default=""),
):
    admin = await current_admin(request)
    if admin is None:
        return _login_redirect()

    from core.db.models import MetaSetupChecklist

    tid = get_current_tenant_id()
    is_done = done == "on"
    async with get_session() as session:
        row = (
            await session.execute(
                select(MetaSetupChecklist).where(
                    MetaSetupChecklist.tenant_id == tid, MetaSetupChecklist.step_key == step_key
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = MetaSetupChecklist(tenant_id=tid, step_key=step_key)
            session.add(row)
        row.done = is_done
        row.notes = notes or None
        row.done_by = admin.id if is_done else None
        row.done_at = utcnow() if is_done else None

    return RedirectResponse("/admin/conexiones/meta-requisitos?flash=Guardado", status_code=303)


# ------------------------------------------------------------------ suscripciones de webhook (app de Meta)
async def _require_owner_or_master(request: Request, tenant_id: uuid.UUID | None) -> bool:
    """"owner/admin" del ticket, traducido a los dos roles elevados que
    existen hoy: `Admin.role == "master"` (superadmin de plataforma) o
    `TenantUser.role == "owner"` del tenant activo -- primer uso real de
    este último campo en una ruta (existía desde el modelo, nunca antes
    verificado)."""
    from core.db.models import TenantUser

    admin = await current_admin(request)
    if admin is None:
        return False
    if admin.role == "master":
        return True
    if tenant_id is None:
        return False
    async with get_session() as session:
        row = (
            await session.execute(
                select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin.id)
            )
        ).scalar_one_or_none()
    return row is not None and row.role == "owner"


async def _webhook_subscriptions_status(tid: uuid.UUID | None) -> list[dict[str, Any]]:
    """Una fila por canal conectado del tenant activo, con el estado de la
    suscripción de APP de la que depende ese canal (no de su suscripción de
    objeto individual, que es otra cosa)."""
    from core.db.models import MetaConnection
    from core.gateway.meta_subscriptions import OBJECT_TO_CHANNEL, list_app_subscriptions

    channel_to_object = {v: k for k, v in OBJECT_TO_CHANNEL.items()}

    if tid is None:
        return []
    async with get_session() as session:
        connections = (
            await session.execute(
                select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.status == "connected")
            )
        ).scalars().all()

    rows: list[dict[str, Any]] = []
    for connection in connections:
        object_ = channel_to_object.get(connection.channel)
        if object_ is None:
            continue
        subs = await list_app_subscriptions(connection.meta_app_id)
        sub = next((s for s in subs if s.object == object_), None)
        rows.append(
            {
                "channel": connection.channel,
                "object": object_,
                "meta_app_id": connection.meta_app_id,
                "callback_url": sub.callback_url if sub else None,
                "active": sub.active if sub else None,
                "fields": sub.fields if sub else [],
                "last_error": sub.last_error if sub else None,
            }
        )
    return rows


@router.get("/conexiones/webhooks", response_class=HTMLResponse)
async def meta_webhooks_page(request: Request):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    rows = await _webhook_subscriptions_status(tid)
    can_manage = await _require_owner_or_master(request, tid)
    ctx.update(subscriptions=rows, can_manage=can_manage)
    return _render("meta_webhooks.html", ctx)


@router.get("/conexiones/webhooks.json")
async def meta_webhooks_json(request: Request):
    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    rows = await _webhook_subscriptions_status(tid)
    return {"subscriptions": rows}


@router.post("/conexiones/webhooks/registrar")
async def meta_webhooks_register(request: Request, channel: str = Form(...)):
    from core.gateway.meta_preflight import resolve_app_credentials
    from core.gateway.meta_subscriptions import (
        OBJECT_TO_CHANNEL,
        SubscriptionError,
        upsert_app_subscription,
    )
    from core.shared.exceptions import AgentCoreError

    tid = get_current_tenant_id()
    if not await _require_owner_or_master(request, tid):
        return HTMLResponse("No tenés permiso para re-registrar suscripciones (se requiere owner o master).", status_code=403)

    channel_to_object = {v: k for k, v in OBJECT_TO_CHANNEL.items()}
    object_ = channel_to_object.get(channel)
    if object_ is None:
        return RedirectResponse(
            f"/admin/conexiones/webhooks?flash=Canal desconocido: {channel}&flash_type=danger", status_code=303
        )

    config = await resolve_app_credentials(tid, channel)
    if config is None:
        return RedirectResponse(
            "/admin/conexiones/webhooks?flash=No hay app de Meta configurada para este canal&flash_type=danger",
            status_code=303,
        )

    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"
    # Esta ruta registra sobre el webhook GENÉRICO (sin slug). Ese handler
    # (`webhooks.py::verify_webhook`) corre sin scope de tenant (ruta pública)
    # y resuelve solo `settings.whatsapp_verify_token` -- el token por tenant
    # vive en la URL por slug y lo maneja el wizard BYOK. Registrar acá el
    # token de la conexión del tenant produce un 403 en el self-check (el
    # handler genérico no lo conoce).
    verify_token = settings.whatsapp_verify_token

    try:
        async with tenant_scope(tid):
            await upsert_app_subscription(config, object_, callback_url=callback_url, verify_token=verify_token)
    except SubscriptionError as exc:
        return RedirectResponse(f"/admin/conexiones/webhooks?flash={str(exc)[:140]}&flash_type=danger", status_code=303)
    except AgentCoreError as exc:  # GraphError
        from core.gateway.meta_subscriptions import humanize_subscription_error

        return RedirectResponse(
            f"/admin/conexiones/webhooks?flash={humanize_subscription_error(exc)[:140]}&flash_type=danger", status_code=303
        )

    return RedirectResponse("/admin/conexiones/webhooks?flash=Suscripción registrada", status_code=303)


# ------------------------------------------------------------------ wizard de Embedded Signup (WhatsApp)
@router.get("/conexiones/wizard/whatsapp/elegir-entorno", response_class=HTMLResponse)
async def meta_wizard_whatsapp_elegir_entorno(request: Request):
    """Pantalla previa al wizard: sin efecto secundario, solo deja elegir
    si esta conexión es de prueba (número gratis de Meta, sin requisitos
    de revisión) o de producción (número real, requiere el checklist de
    /admin/conexiones/meta-requisitos)."""
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    from core.db.models import Tenant
    from core.tenants import tenant_webhook_url

    tenant_slug = ""
    tid = get_current_tenant_id()
    if tid is not None:
        async with get_session() as session:
            tenant = await session.get(Tenant, tid)
            tenant_slug = tenant.slug if tenant else ""

    settings = get_settings()
    ctx.update(
        webhook_url=tenant_webhook_url(tenant_slug) if tenant_slug else "",
        whatsapp_verify_token=settings.whatsapp_verify_token,
    )
    return _render("meta_wizard_elegir_entorno.html", ctx)


@router.get("/conexiones/wizard/whatsapp", response_class=HTMLResponse)
async def meta_wizard_whatsapp_page(request: Request, environment: str = Query(default="production")):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    if environment not in ("sandbox", "production"):
        return RedirectResponse("/admin/conexiones/wizard/whatsapp/elegir-entorno", status_code=303)

    from core.gateway.meta_onboarding import (
        WizardError,
        run_automatic_steps,
        start_or_resume_wizard,
    )

    admin = await current_admin(request)
    tid = get_current_tenant_id()

    try:
        wizard = await start_or_resume_wizard(
            tid, "whatsapp", environment=environment, admin_id=admin.id if admin else None
        )
        wizard = await run_automatic_steps(wizard.id)
    except WizardError as exc:
        ctx.update(setup_error=str(exc), wizard=None)
        return _render("meta_wizard_whatsapp.html", ctx)

    settings = get_settings()
    ctx.update(
        setup_error=None,
        wizard=wizard,
        wizard_steps=list(WIZARD_STEPS),
        meta_app_id=settings.meta_app_id,
        meta_config_id=settings.meta_config_id,
        graph_api_version=settings.graph_api_version,
    )

    if environment == "production":
        manual_steps = await _production_manual_steps(tid)
        ctx.update(production_requisitos_completos=all(step["done"] for step in manual_steps))
    else:
        ctx.update(production_requisitos_completos=None)

    if wizard.current_step == "done" and wizard.connection_id is not None:
        async with get_session() as session:
            waba = (
                await session.execute(
                    select(WabaAccount).where(
                        WabaAccount.connection_id == wizard.connection_id, WabaAccount.tenant_id == tid
                    )
                )
            ).scalars().first()
            phone = (
                await session.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.connection_id == wizard.connection_id, PhoneNumber.tenant_id == tid
                    )
                )
            ).scalars().first()
            template_count = (
                await session.execute(
                    select(func.count()).select_from(MessageTemplate).where(
                        MessageTemplate.waba_account_id == waba.id
                    )
                )
            ).scalar() if waba is not None else 0
        ctx.update(summary_waba=waba, summary_phone=phone, summary_template_count=template_count)

    return _render("meta_wizard_whatsapp.html", ctx)


@router.post("/conexiones/wizard/whatsapp/probar-conexion")
async def meta_wizard_whatsapp_probar_conexion(request: Request, connection_id: str = Form(...)):
    """Verificación real bajo demanda (no espera al cron de salud): handshake
    de webhook en vivo + los 7 health checks de la conexión (mismo criterio
    que el botón "Diagnosticar" de /admin/conexiones/salud, reusa las mismas
    funciones -- no se agrega ningún check nuevo)."""
    from core.gateway import meta_production_readiness as readiness
    from core.gateway.meta_health import decide_status, run_health_checks

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    url = "/admin/conexiones/wizard/whatsapp"

    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
    if connection is None:
        return RedirectResponse(f"{url}?flash=Conexión no encontrada&flash_type=danger", status_code=303)

    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"
    handshake = await readiness.check_webhook_handshake(callback_url, settings.whatsapp_verify_token)
    checks = await run_health_checks(connection, tid)
    new_status, reason = decide_status(checks)

    async with get_session() as session:
        db_connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
        if db_connection is not None:
            db_connection.health = checks
            db_connection.status = new_status
            db_connection.status_reason = reason
            db_connection.last_health_check_at = utcnow()

    todo_ok = handshake.ok is True and all(v.get("ok") is True for v in checks.values() if v.get("key") != "billing")
    flash = "Prueba de conexión OK" if todo_ok else "La prueba de conexión encontró problemas -- revisá el resumen"
    flash_type = "success" if todo_ok else "warning"
    return RedirectResponse(
        f"{url}?environment={connection.environment}&flash={flash}&flash_type={flash_type}", status_code=303
    )


@router.post("/conexiones/wizard/whatsapp-byok/guardar")
async def meta_wizard_whatsapp_byok_guardar(
    request: Request,
    app_id: str = Form(...),
    app_secret: str = Form(...),
    phone_number_id: str = Form(...),
    access_token: str = Form(...),
    waba_id: str = Form(...),
    verify_token: str = Form(default=""),
):
    """Alta de una conexión de WhatsApp con la app PROPIA del tenant (BYOK)
    para modo prueba -- a diferencia del wizard de Embedded Signup, acá no
    hay OAuth: se verifican las credenciales contra Graph API y se guardan
    directo (mismo criterio que el modal legado de ChannelConnection, pero
    persistiendo en MetaConnection para que el resto del panel -- salud,
    promoción a producción -- la vea igual que una conexión de Embedded Signup."""
    from core.gateway.meta_byok import ByokError, save_byok_connection

    admin = await current_admin(request)
    if admin is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    elegir_url = "/admin/conexiones/wizard/whatsapp/elegir-entorno"

    try:
        await save_byok_connection(
            tid, admin.id, app_id.strip(), app_secret.strip(), phone_number_id.strip(), access_token.strip(),
            waba_id.strip(), verify_token=verify_token.strip() or None,
        )
    except ByokError as exc:
        return RedirectResponse(f"{elegir_url}?flash={str(exc)[:140]}&flash_type=danger", status_code=303)

    return RedirectResponse(
        "/admin/conexiones/salud?flash=Conexión de prueba guardada -- probála con \"Diagnosticar\"",
        status_code=303,
    )


@router.post("/conexiones/wizard/whatsapp-byok/token/inspect")
async def meta_byok_token_inspect(
    request: Request,
    app_id: str = Form(...),
    app_secret: str = Form(...),
    access_token: str = Form(...),
):
    """Verifica un access token contra Meta (`/debug_token`): validez,
    expiración (0 = no expira) y scopes. Para el asistente del wizard BYOK.
    Nunca loguea el token."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_token_tools import inspect_token

    if await current_admin(request) is None:
        return JSONResponse({"ok": False, "error": "no autenticado"}, status_code=401)
    if not app_id.strip() or not app_secret.strip() or not access_token.strip():
        return JSONResponse({"ok": False, "error": "Faltan App ID, App Secret o token"}, status_code=200)
    try:
        info = await inspect_token(app_id.strip(), app_secret.strip(), access_token.strip())
    except GraphError as exc:
        return JSONResponse({"ok": False, "error": exc.message}, headers=_NO_STORE)
    return JSONResponse({"ok": True, **info}, headers=_NO_STORE)


@router.post("/conexiones/wizard/whatsapp-byok/token/extend")
async def meta_byok_token_extend(
    request: Request,
    app_id: str = Form(...),
    app_secret: str = Form(...),
    access_token: str = Form(...),
):
    """Canjea un token temporal por uno long-lived (~60 días). Devuelve el
    token nuevo para que el wizard lo ponga en el campo (nunca se loguea)."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_token_tools import extend_token

    if await current_admin(request) is None:
        return JSONResponse({"ok": False, "error": "no autenticado"}, status_code=401)
    if not app_id.strip() or not app_secret.strip() or not access_token.strip():
        return JSONResponse({"ok": False, "error": "Faltan App ID, App Secret o token"}, status_code=200)
    try:
        result = await extend_token(app_id.strip(), app_secret.strip(), access_token.strip())
    except GraphError as exc:
        return JSONResponse({"ok": False, "error": exc.message}, headers=_NO_STORE)
    return JSONResponse({"ok": True, **result}, headers=_NO_STORE)


@router.post("/conexiones/wizard/whatsapp/exchange")
async def meta_wizard_whatsapp_exchange(request: Request):
    """Llamada por el listener de `postMessage` apenas llega el evento
    `FINISH*` -- el `code` de Meta tiene TTL de 30s, así que esto tiene que
    responder rápido. Nunca devuelve el token, solo `{connectionId, nextStep}`."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import WizardError, exchange_code, persist_finish_payload

    if await current_admin(request) is None:
        return JSONResponse({"error": "no autenticado"}, status_code=401)

    body = await request.json()
    wizard_id = body.get("wizard_id")
    code = body.get("code")
    event_type = body.get("event_type", "FINISH")
    if not wizard_id or not code:
        return JSONResponse({"error": "faltan wizard_id/code"}, status_code=400)

    try:
        wizard_id_uuid = uuid.UUID(str(wizard_id))
        view = await persist_finish_payload(
            wizard_id_uuid,
            event_type,
            waba_id=body.get("waba_id"),
            phone_number_id=body.get("phone_number_id"),
            business_id=body.get("business_id"),
        )
        view = await exchange_code(view.id, str(code))
    except WizardError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except GraphError as exc:
        from core.gateway.meta_subscriptions import humanize_subscription_error

        return JSONResponse(
            {"error": humanize_subscription_error(exc), "fbtraceId": exc.fbtrace_id}, status_code=502
        )

    return JSONResponse({"connectionId": str(view.connection_id), "nextStep": view.current_step})


@router.post("/conexiones/wizard/whatsapp/cancelar-evento")
async def meta_wizard_whatsapp_cancel(request: Request):
    """Beacon del listener en el evento `CANCEL` -- analítica de abandono,
    nunca lleva `code` ni token (la función de destino ni acepta esos campos)."""
    from core.gateway.meta_onboarding import record_cancel_or_progress

    if await current_admin(request) is None:
        return JSONResponse({"error": "no autenticado"}, status_code=401)

    body = await request.json()
    wizard_id = body.get("wizard_id")
    if not wizard_id:
        return JSONResponse({"error": "falta wizard_id"}, status_code=400)

    await record_cancel_or_progress(
        uuid.UUID(str(wizard_id)),
        current_step=body.get("current_step"),
        error_code=body.get("error_code"),
        error_message=body.get("error_message"),
        error_id=body.get("error_id"),
        session_id=body.get("session_id"),
    )
    return JSONResponse({"status": "ok"})


@router.post("/conexiones/wizard/whatsapp/registrar-numero")
async def meta_wizard_whatsapp_register_number(request: Request, wizard_id: str = Form(...)):
    """El PIN lo genera el sistema -- este form no lleva ningún campo de
    PIN, solo confirma la acción. El PIN nunca aparece en esta respuesta."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import WizardError, register_number_step, run_automatic_steps

    if await current_admin(request) is None:
        return _login_redirect()

    try:
        view = await register_number_step(uuid.UUID(wizard_id))
        await run_automatic_steps(view.id)
    except (WizardError, GraphError) as exc:
        detail = exc.message if isinstance(exc, GraphError) else str(exc)
        return RedirectResponse(
            f"/admin/conexiones/wizard/whatsapp?flash={detail[:140]}&flash_type=danger", status_code=303
        )

    return RedirectResponse("/admin/conexiones/wizard/whatsapp", status_code=303)


@router.post("/conexiones/wizard/whatsapp/fijar-pin-nuevo")
async def meta_wizard_whatsapp_set_new_pin(request: Request, wizard_id: str = Form(...)):
    """Remediación manual de un 133005 (PIN mismatch) -- nunca automática."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import WizardError, run_automatic_steps, set_new_pin_step

    if await current_admin(request) is None:
        return _login_redirect()

    try:
        view = await set_new_pin_step(uuid.UUID(wizard_id))
        await run_automatic_steps(view.id)
    except (WizardError, GraphError) as exc:
        detail = exc.message if isinstance(exc, GraphError) else str(exc)
        return RedirectResponse(
            f"/admin/conexiones/wizard/whatsapp?flash={detail[:140]}&flash_type=danger", status_code=303
        )

    return RedirectResponse("/admin/conexiones/wizard/whatsapp?flash=PIN nuevo fijado", status_code=303)


# ------------------------------------------------------------------ números adicionales de una WABA
# Prefijo "meta/" para no chocar con /conexiones/{connection_id}/... de
# arriba, que son rutas de ChannelConnection (modelo viejo/distinto).
async def _resolve_waba_and_token(connection_id: uuid.UUID, tenant_id: uuid.UUID | None):
    from core.db.models import MetaConnection, WabaAccount
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND

    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, connection_id, tenant_id)
        if connection is None:
            return None, None, None
        waba = (
            await session.execute(select(WabaAccount).where(WabaAccount.connection_id == connection_id))
        ).scalars().first()
    if waba is None:
        return connection, None, None
    token = await meta_token_store.get_token(
        connection_id, TOKEN_KIND, connection.business_id or str(connection_id), tenant_id=tenant_id
    )
    return connection, waba, token


@router.get("/conexiones/meta/{connection_id}/numeros", response_class=HTMLResponse)
async def meta_connection_numeros_page(request: Request, connection_id: str):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    from core.db.models import PhoneNumber
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import sync_phone_numbers

    tid = get_current_tenant_id()
    connection, waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    if connection is None:
        return RedirectResponse("/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303)

    sync_error = None
    if waba is not None and token:
        try:
            await sync_phone_numbers(
                waba.waba_id, token, get_settings().meta_app_secret, connection.graph_api_version, connection.id, tid
            )
        except GraphError as exc:
            sync_error = exc.message

    async with get_session() as session:
        numbers = (
            await session.execute(
                select(PhoneNumber).where(PhoneNumber.connection_id == connection.id, PhoneNumber.tenant_id == tid)
            )
        ).scalars().all()

    ctx.update(connection=connection, waba=waba, numbers=numbers, sync_error=sync_error)
    return _render("meta_numeros.html", ctx)


@router.post("/conexiones/meta/{connection_id}/numeros/agregar")
async def meta_connection_numeros_agregar(
    request: Request, connection_id: str, cc: str = Form(...), phone_number: str = Form(...), verified_name: str = Form(...)
):
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import add_phone_number

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/numeros"
    if connection is None or waba is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión/WABA lista todavía&flash_type=danger", status_code=303)

    try:
        await add_phone_number(
            waba.waba_id, token, get_settings().meta_app_secret, connection.graph_api_version, connection.id, tid,
            cc=cc, phone_number=phone_number, verified_name=verified_name,
        )
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:140]}&flash_type=danger", status_code=303)

    return RedirectResponse(f"{url}?flash=Número agregado", status_code=303)


@router.post("/conexiones/meta/{connection_id}/numeros/{phone_number_id}/solicitar-codigo")
async def meta_connection_numeros_solicitar_codigo(
    request: Request, connection_id: str, phone_number_id: str, code_method: str = Form(...), language: str = Form(default="es")
):
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import request_verification_code
    from core.memory.working import get_redis

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, _waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/numeros"
    if connection is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión lista todavía&flash_type=danger", status_code=303)

    redis = get_redis()
    # Namespaced por tenant -- phone_number_id es único global en Meta pero
    # no queremos que un tenant pueda interferir el rate limit de otro
    # (hallazgo auditor-multitenant, 2026-09-22, severidad media).
    rate_limit_key = f"verify_code_resend:{tid}:{phone_number_id}"
    if await redis.get(rate_limit_key):
        return RedirectResponse(f"{url}?flash=Esperá un momento antes de reenviar el código&flash_type=danger", status_code=303)

    try:
        await request_verification_code(
            phone_number_id, token, get_settings().meta_app_secret, connection.graph_api_version, connection.id,
            code_method=code_method, language=language,
        )
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:140]}&flash_type=danger", status_code=303)

    await redis.set(rate_limit_key, "1", ex=60)
    return RedirectResponse(f"{url}?flash=Código enviado", status_code=303)


@router.post("/conexiones/meta/{connection_id}/numeros/{phone_number_id}/verificar")
async def meta_connection_numeros_verificar(request: Request, connection_id: str, phone_number_id: str, code: str = Form(...)):
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_phone_numbers import verify_code

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, _waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/numeros"
    if connection is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión lista todavía&flash_type=danger", status_code=303)

    try:
        ok = await verify_code(
            phone_number_id, token, get_settings().meta_app_secret, connection.graph_api_version, connection.id, tid, code=code
        )
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:140]}&flash_type=danger", status_code=303)

    if not ok:
        return RedirectResponse(f"{url}?flash=Código incorrecto&flash_type=danger", status_code=303)
    return RedirectResponse(f"{url}?flash=Número verificado", status_code=303)


# ---------------------------------------------------------- plantillas de WhatsApp
@router.get("/conexiones/meta/{connection_id}/plantillas", response_class=HTMLResponse)
async def meta_connection_plantillas_page(request: Request, connection_id: str):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    from core.db.models import MessageTemplate
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates

    tid = get_current_tenant_id()
    connection, waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    if connection is None:
        return RedirectResponse("/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303)

    sync_error = None
    if waba is not None and token and await acquire_sync_lock(waba.waba_id):
        try:
            await sync_templates(
                waba.waba_id, token, get_settings().meta_app_secret, connection.graph_api_version, connection.id, tid
            )
        except GraphError as exc:
            sync_error = exc.message

    templates: list[MessageTemplate] = []
    if waba is not None:
        async with get_session() as session:
            templates = (
                await session.execute(
                    select(MessageTemplate)
                    .where(MessageTemplate.waba_account_id == waba.id, MessageTemplate.tenant_id == tid)
                    .order_by(MessageTemplate.name, MessageTemplate.language)
                )
            ).scalars().all()

    at_limit = bool(
        waba is not None and waba.message_template_limit is not None
        and (waba.message_template_count or 0) >= waba.message_template_limit
    )

    ctx.update(connection=connection, waba=waba, templates=templates, sync_error=sync_error, at_limit=at_limit)
    return _render("meta_plantillas.html", ctx)


@router.get("/conexiones/meta/{connection_id}/plantillas/nueva", response_class=HTMLResponse)
async def meta_connection_plantilla_nueva_page(request: Request, connection_id: str):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, _token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    if connection is None or waba is None:
        return RedirectResponse(
            f"/admin/conexiones/meta/{connection_id}/plantillas?flash=No hay WABA lista todavía&flash_type=danger",
            status_code=303,
        )

    ctx.update(connection=connection, waba=waba)
    return _render("meta_plantilla_nueva.html", ctx)


def _build_template_components(form: dict[str, str]) -> list[dict[str, Any]]:
    """Arma `components` a partir de los campos planos del formulario --
    permite validar param_name<->texto ANTES de serializar nada."""
    import json

    components: list[dict[str, Any]] = []
    parameter_format = form.get("parameter_format") or None

    header_format = form.get("header_format", "NONE")
    if header_format == "TEXT" and form.get("header_text"):
        header: dict[str, Any] = {"type": "HEADER", "format": "TEXT", "text": form["header_text"]}
        if parameter_format == "NAMED" and form.get("header_param_name"):
            header["example"] = {
                "header_text_named_params": [
                    {"param_name": form["header_param_name"], "example": form.get("header_param_example", "")}
                ]
            }
        elif form.get("header_param_example"):
            header["example"] = {"header_text": [form["header_param_example"]]}
        components.append(header)
    elif header_format in ("IMAGE", "VIDEO", "DOCUMENT") and form.get("header_handle"):
        components.append(
            {"type": "HEADER", "format": header_format, "example": {"header_handle": [form["header_handle"]]}}
        )

    body: dict[str, Any] = {"type": "BODY", "text": form.get("body_text", "")}
    if parameter_format == "NAMED" and form.get("body_param_names"):
        names = [n.strip() for n in form["body_param_names"].split(",") if n.strip()]
        examples = [e.strip() for e in form.get("body_param_examples", "").split(",")]
        body["example"] = {
            "body_text_named_params": [
                {"param_name": name, "example": examples[i] if i < len(examples) else ""}
                for i, name in enumerate(names)
            ]
        }
    elif form.get("body_param_examples"):
        examples = [e.strip() for e in form["body_param_examples"].split(",") if e.strip()]
        if examples:
            body["example"] = {"body_text": [examples]}
    components.append(body)

    if form.get("footer_text"):
        components.append({"type": "FOOTER", "text": form["footer_text"]})

    buttons_json = form.get("buttons_json")
    if buttons_json:
        buttons = json.loads(buttons_json)
        if buttons:
            components.append({"type": "BUTTONS", "buttons": buttons})

    return components


@router.post("/conexiones/meta/{connection_id}/plantillas/nueva")
async def meta_connection_plantilla_crear(request: Request, connection_id: str):
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_preflight import resolve_app_credentials
    from core.gateway.meta_subscriptions import fetch_app_access_token
    from core.gateway.meta_template_upload import (
        TemplateUploadError,
        start_upload_session,
        upload_file_chunk,
    )
    from core.gateway.meta_templates import (
        TemplateRateLimitError,
        TemplateValidationError,
        create_template,
    )

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    if connection is None or waba is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión/WABA lista todavía&flash_type=danger", status_code=303)

    form = dict(await request.form())
    header_format = form.get("header_format", "NONE")

    try:
        if header_format in ("IMAGE", "VIDEO", "DOCUMENT"):
            upload = form.get("header_file")
            if upload is None or not hasattr(upload, "read"):
                raise TemplateValidationError("HEADER: falta el archivo de cabecera")
            file_bytes = await upload.read()
            app_creds = await resolve_app_credentials(tid, channel="whatsapp")
            if app_creds is None:
                raise TemplateValidationError("no hay credenciales de app configuradas para subir la cabecera")
            app_access_token = await fetch_app_access_token(
                app_creds.app_id, app_creds.app_secret, app_creds.graph_api_version
            )
            upload_id = await start_upload_session(
                app_creds.app_id, app_access_token, app_creds.app_secret, app_creds.graph_api_version, connection.id,
                file_name=upload.filename or "cabecera", file_length=len(file_bytes),
                file_type=upload.content_type or "application/octet-stream",
            )
            handle = await upload_file_chunk(upload_id, app_access_token, file_bytes)
            form["header_handle"] = handle

        components = _build_template_components(form)
        await create_template(
            waba.waba_id, token, get_settings().meta_app_secret, connection.graph_api_version, connection.id, tid,
            waba.id, name=form["name"], language=form["language"], category=form["category"],
            components=components, parameter_format=form.get("parameter_format") or None,
        )
    except (TemplateValidationError, TemplateRateLimitError, TemplateUploadError) as exc:
        return RedirectResponse(f"{url}?flash={str(exc)[:200]}&flash_type=danger", status_code=303)
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:200]}&flash_type=danger", status_code=303)

    return RedirectResponse(f"{url}?flash=Plantilla creada, pendiente de revisión de Meta", status_code=303)


@router.post("/conexiones/meta/{connection_id}/plantillas/{template_id}/editar")
async def meta_connection_plantilla_editar(request: Request, connection_id: str, template_id: str, category: str = Form(None)):
    from core.db.models import MessageTemplate
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_templates import TemplateValidationError, update_template

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, _waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    if connection is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión lista todavía&flash_type=danger", status_code=303)

    async with get_session() as session:
        row = await get_owned(session, MessageTemplate, uuid.UUID(template_id), tid)
    if row is None or not row.meta_template_id:
        return RedirectResponse(f"{url}?flash=Plantilla no encontrada&flash_type=danger", status_code=303)

    try:
        await update_template(
            row.meta_template_id, token, get_settings().meta_app_secret, connection.graph_api_version,
            connection.id, tid, category=category or None,
        )
    except TemplateValidationError as exc:
        return RedirectResponse(f"{url}?flash={str(exc)[:200]}&flash_type=danger", status_code=303)
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:200]}&flash_type=danger", status_code=303)

    return RedirectResponse(f"{url}?flash=Plantilla actualizada", status_code=303)


@router.post("/conexiones/meta/{connection_id}/plantillas/{template_id}/eliminar")
async def meta_connection_plantilla_eliminar(request: Request, connection_id: str, template_id: str):
    """Borra TODAS las versiones de idioma de la plantilla."""
    from core.db.models import MessageTemplate
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_templates import delete_template_all_languages

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    if connection is None or waba is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión/WABA lista todavía&flash_type=danger", status_code=303)

    async with get_session() as session:
        row = await get_owned(session, MessageTemplate, uuid.UUID(template_id), tid)
    if row is None:
        return RedirectResponse(f"{url}?flash=Plantilla no encontrada&flash_type=danger", status_code=303)

    try:
        await delete_template_all_languages(
            waba.waba_id, row.name, token, get_settings().meta_app_secret, connection.graph_api_version, connection.id, tid
        )
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:200]}&flash_type=danger", status_code=303)

    return RedirectResponse(f"{url}?flash=Plantilla eliminada (todos los idiomas)", status_code=303)


@router.post("/conexiones/meta/{connection_id}/plantillas/{template_id}/eliminar-traduccion")
async def meta_connection_plantilla_eliminar_traduccion(request: Request, connection_id: str, template_id: str):
    """Borra SOLO esta versión de idioma."""
    from core.db.models import MessageTemplate
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_templates import delete_template_one_language

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    if connection is None or waba is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión/WABA lista todavía&flash_type=danger", status_code=303)

    async with get_session() as session:
        row = await get_owned(session, MessageTemplate, uuid.UUID(template_id), tid)
    if row is None or not row.meta_template_id:
        return RedirectResponse(f"{url}?flash=Plantilla no encontrada&flash_type=danger", status_code=303)

    try:
        await delete_template_one_language(
            waba.waba_id, row.name, row.meta_template_id, token, get_settings().meta_app_secret,
            connection.graph_api_version, connection.id, tid,
        )
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:200]}&flash_type=danger", status_code=303)

    return RedirectResponse(f"{url}?flash=Traducción eliminada ({row.language})", status_code=303)


@router.post("/conexiones/meta/{connection_id}/plantillas/sync")
async def meta_connection_plantillas_sync(request: Request, connection_id: str):
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_sync import acquire_sync_lock, sync_templates

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    if connection is None or waba is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión/WABA lista todavía&flash_type=danger", status_code=303)

    if not await acquire_sync_lock(waba.waba_id):
        return RedirectResponse(f"{url}?flash=Ya se sincronizó en la última hora&flash_type=danger", status_code=303)

    try:
        await sync_templates(
            waba.waba_id, token, get_settings().meta_app_secret, connection.graph_api_version, connection.id, tid
        )
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:200]}&flash_type=danger", status_code=303)

    return RedirectResponse(f"{url}?flash=Sincronizado", status_code=303)


# ------------------------------------------------------------------ salud de conexiones Meta
@router.get("/conexiones/salud", response_class=HTMLResponse)
async def meta_health_page(request: Request):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    async with get_session() as session:
        connections = (
            await session.execute(
                select(MetaConnection).where(
                    MetaConnection.tenant_id == tid, MetaConnection.deleted_at.is_(None)
                )
            )
        ).scalars().all()

    # Expiración del access token por conexión (badge de aviso antes de que venza).
    from core.gateway.meta_byok import CLOUD_API_ACCESS_TOKEN_KIND
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.gateway.meta_token_store import get_token_expires_at

    token_expiry: dict = {}
    for connection in connections:
        exp = await get_token_expires_at(
            connection.id, CLOUD_API_ACCESS_TOKEN_KIND, connection.meta_app_id, tenant_id=tid
        )
        if exp is None:
            exp = await get_token_expires_at(
                connection.id, TOKEN_KIND, connection.business_id or str(connection.id), tenant_id=tid
            )
        token_expiry[connection.id] = exp

    ctx.update(connections=connections, token_expiry=token_expiry, now=datetime.now(UTC))
    return _render("meta_salud.html", ctx)


@router.post("/conexiones/meta/{connection_id}/activar")
async def meta_connection_activar(request: Request, connection_id: str):
    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
        if connection is None:
            return RedirectResponse("/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303)
        connection.status = "connected"
        connection.status_reason = None

    return RedirectResponse("/admin/conexiones?flash=Conexión activada", status_code=303)


@router.post("/conexiones/meta/{connection_id}/desactivar")
async def meta_connection_desactivar(request: Request, connection_id: str):
    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
        if connection is None:
            return RedirectResponse("/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303)
        connection.status = "revoked"
        connection.status_reason = "Desactivada a mano desde el panel"

    return RedirectResponse("/admin/conexiones?flash=Conexión desactivada", status_code=303)


@router.post("/conexiones/meta/{connection_id}/eliminar")
async def meta_connection_eliminar(request: Request, connection_id: str):
    """Soft-delete (invariante nº8 de CLAUDE.md) -- nunca borra la fila,
    solo la saca de la lista y libera el `meta_app_id` para poder
    reconectar la misma app después (índice único parcial, migración
    0032)."""
    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
        if connection is None:
            return RedirectResponse("/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303)
        connection.deleted_at = utcnow()
        connection.status = "revoked"

    return RedirectResponse("/admin/conexiones?flash=Conexión eliminada", status_code=303)


@router.post("/conexiones/salud/{connection_id}/diagnosticar")
async def meta_health_diagnosticar(request: Request, connection_id: str):
    """Diagnóstico on-demand -- corre los 7 checks síncrono (debe responder
    en <10s para 1 WABA + 2 números, criterio de aceptación del ticket)."""
    from core.gateway.meta_health import decide_status, run_health_checks

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    url = "/admin/conexiones/salud"
    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
    if connection is None:
        return RedirectResponse(f"{url}?flash=Conexión no encontrada&flash_type=danger", status_code=303)

    checks = await run_health_checks(connection, tid)
    new_status, reason = decide_status(checks)

    async with get_session() as session:
        db_connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
        if db_connection is not None:
            db_connection.health = checks
            db_connection.status = new_status
            db_connection.status_reason = reason
            db_connection.last_health_check_at = utcnow()

    return RedirectResponse(f"{url}?flash=Diagnóstico completo", status_code=303)


@router.post("/conexiones/salud/{connection_id}/resubscribe-waba")
async def meta_health_resubscribe_waba(request: Request, connection_id: str):
    """Reparación INLINE del check `webhook_subscribed` -- reencola SOLO
    la suscripción de la WABA, nunca el wizard de OAuth completo."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import _subscribe_waba_to_app

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    url = "/admin/conexiones/salud"
    connection, waba, token = await _resolve_waba_and_token(uuid.UUID(connection_id), tid)
    if connection is None or waba is None or not token:
        return RedirectResponse(f"{url}?flash=No hay conexión/WABA lista todavía&flash_type=danger", status_code=303)

    try:
        await _subscribe_waba_to_app(
            token, waba.waba_id, connection.graph_api_version, connection.id, get_settings().meta_app_secret, tid
        )
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:200]}&flash_type=danger", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"{url}?flash={str(exc)[:200]}&flash_type=danger", status_code=303)

    return RedirectResponse(f"{url}?flash=WABA re-suscripta", status_code=303)


@router.post("/conexiones/salud/{connection_id}/billing")
async def meta_health_set_billing_status(request: Request, connection_id: str, manual_status: str = Form(...)):
    """El único "repair" que no llama a Meta -- billing no tiene API, esto
    solo persiste lo que un admin marcó a mano después de configurarlo en
    WhatsApp Manager (ver /admin/costos/meta/creditos)."""
    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    url = "/admin/conexiones/salud"
    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
        if connection is None:
            return RedirectResponse(f"{url}?flash=Conexión no encontrada&flash_type=danger", status_code=303)
        health = dict(connection.health or {})
        health["billing"] = {"manual_status": manual_status}
        connection.health = health

    return RedirectResponse(f"{url}?flash=Estado de facturación actualizado", status_code=303)


@router.get("/conexiones/manual", response_class=HTMLResponse)
async def meta_manual_connection_page(request: Request):
    """Ruta de escape: cargar waba_id/phone_number_id/token a mano cuando
    el Embedded Signup falla -- deliberadamente frágil, es la salida en
    self-hosted cuando el wizard normal no funciona."""
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()
    return _render("meta_manual.html", ctx)


@router.post("/conexiones/manual")
async def meta_manual_connection_submit(
    request: Request, waba_id: str = Form(...), phone_number_id: str = Form(...),
    token: str = Form(...), business_id: str = Form(""),
):
    from core.gateway import meta_token_store
    from core.gateway.meta_health import check_token_valid
    from core.gateway.meta_onboarding import TOKEN_KIND

    admin = await current_admin(request)
    if admin is None:
        return _login_redirect()

    from core.gateway.meta_preflight import resolve_app_credentials

    tid = get_current_tenant_id()
    url = "/admin/conexiones/manual"
    settings = get_settings()

    # `debug_token` necesita las credenciales de LA APP que emitió el
    # token -- si el tenant es BYOK (tiene su propia app configurada), se
    # usan esas; si no, cae a la app compartida de `.env` (hallazgo
    # auditor-multitenant 2026-09-20: antes usaba siempre la global sin
    # considerar BYOK).
    app_creds = await resolve_app_credentials(tid, "whatsapp")
    app_id = app_creds.app_id if app_creds else settings.meta_app_id
    app_secret = app_creds.app_secret if app_creds else settings.meta_app_secret

    # Valida el token ANTES de guardar nada -- no se persiste un token roto a ciegas.
    check = await check_token_valid(token, app_id, app_secret, settings.graph_api_version)
    if check.ok is False:
        return RedirectResponse(f"{url}?flash={check.detail[:200]}&flash_type=danger", status_code=303)

    async with get_session() as session:
        connection = MetaConnection(
            tenant_id=tid, channel="whatsapp", meta_app_id=app_id,
            business_id=business_id or None, status="connected", connected_by=admin.id,
            connected_at=utcnow(),
        )
        session.add(connection)
        await session.flush()
        waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id)
        session.add(waba)
        await session.flush()
        session.add(
            PhoneNumber(tenant_id=tid, connection_id=connection.id, waba_account_id=waba.id, phone_number_id=phone_number_id)
        )
        connection_id = connection.id

    await meta_token_store.put_token(connection_id, TOKEN_KIND, business_id or str(connection_id), token, tenant_id=tid)

    return RedirectResponse("/admin/conexiones/salud?flash=Conexión manual creada", status_code=303)


# ------------------------------------------------------------------ checklist de promoción a producción
async def _production_manual_steps(tid: uuid.UUID) -> list[dict[str, Any]]:
    from core.db.models import MetaSetupChecklist
    from core.gateway.meta_preflight import MANUAL_STEPS, resolve_app_credentials

    config = await resolve_app_credentials(tid)
    app_dashboard_base = (
        f"https://developers.facebook.com/apps/{config.app_id}/" if config else "https://developers.facebook.com/apps/"
    )
    async with get_session() as session:
        done_rows = (
            await session.execute(select(MetaSetupChecklist).where(MetaSetupChecklist.tenant_id == tid))
        ).scalars().all()
    done_by_key = {row.step_key: row for row in done_rows}

    return [
        {
            "key": step["key"],
            "label": step["label"],
            "link": app_dashboard_base + step["link_suffix"] if config else "https://developers.facebook.com/apps/",
            "done": done_by_key[step["key"]].done if step["key"] in done_by_key else False,
            "notes": done_by_key[step["key"]].notes if step["key"] in done_by_key else "",
        }
        for step in MANUAL_STEPS
    ]


@router.get("/conexiones/produccion", response_class=HTMLResponse)
async def meta_produccion_page(request: Request):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    manual_steps = await _production_manual_steps(tid)
    ctx.update(manual_steps=manual_steps, auto_checks=None)
    return _render("meta_produccion.html", ctx)


@router.post("/conexiones/produccion/ejecutar", response_class=HTMLResponse)
async def meta_produccion_ejecutar(request: Request):
    """Corre los 9 checks automáticos on-demand. La mayoría solo dan una
    respuesta con sentido contra un despliegue de producción real -- en
    cualquier otro entorno (incluida esta misma instancia si no es
    producción) van a dar `False`/`None`, y el panel lo muestra tal cual,
    sin disfrazarlo."""
    from core.gateway import meta_production_readiness as readiness

    if await current_admin(request) is None:
        return _login_redirect()

    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"
    verify_token = settings.whatsapp_verify_token

    auto_checks = []
    auto_checks.append(await readiness.check_webhook_handshake(callback_url, verify_token))
    auto_checks.append(await readiness.check_app_subscriptions_complete(settings.meta_app_id))
    auto_checks.append(readiness.check_prod_secret_differs_from_dev(settings.meta_app_secret))
    auto_checks.append(readiness.check_token_keys_configured())
    auto_checks.append(await readiness.check_worker_heartbeat_and_dlq())
    auto_checks.append(await readiness.check_rls_forced())
    auto_checks.append(readiness.check_rls_tests_pass())
    auto_checks.append(readiness.check_no_secret_leak_in_templates())
    auto_checks.append(await readiness.check_connections_health_except_billing(tid))

    manual_steps = await _production_manual_steps(tid)
    ctx.update(manual_steps=manual_steps, auto_checks=[c.model_dump(mode="json") for c in auto_checks])
    return _render("meta_produccion.html", ctx)


# ------------------------------------------------------------------ costos
@router.get("/costos", response_class=HTMLResponse)
async def costs_page(request: Request):
    ctx = await _base_context(request, "costs")
    if ctx is None:
        return _login_redirect()
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_ago = now - timedelta(days=30)
    tenant_id = get_current_tenant_id()
    # Filtro explícito por tenant (además de RLS): el panel es multi-tenant.
    by_tenant = UsageLedger.tenant_id == tenant_id

    async with get_session() as session:
        today_cost = (
            await session.execute(
                select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
                    by_tenant, UsageLedger.created_at >= today_start
                )
            )
        ).scalar() or 0
        month_cost = (
            await session.execute(
                select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
                    by_tenant, UsageLedger.created_at >= month_ago
                )
            )
        ).scalar() or 0
        total_turns = (
            await session.execute(select(func.count(UsageLedger.id)).where(by_tenant))
        ).scalar() or 0
        by_model_rows = (
            await session.execute(
                select(
                    UsageLedger.model,
                    func.count(UsageLedger.id),
                    func.sum(UsageLedger.input_tokens),
                    func.sum(UsageLedger.output_tokens),
                    func.avg(UsageLedger.latency_ms),
                    func.sum(UsageLedger.cost_usd),
                )
                .where(by_tenant, UsageLedger.created_at >= month_ago)
                .group_by(UsageLedger.model)
            )
        ).all()
        by_conversation_rows = (
            await session.execute(
                select(
                    UsageLedger.conversation_id,
                    func.count(UsageLedger.id),
                    func.sum(UsageLedger.cost_usd),
                    func.max(UsageLedger.created_at),
                )
                .where(by_tenant, UsageLedger.created_at >= month_ago)
                .group_by(UsageLedger.conversation_id)
                .order_by(func.sum(UsageLedger.cost_usd).desc())
                .limit(20)
            )
        ).all()

    ctx.update(
        today=float(today_cost),
        month=float(month_cost),
        total_turns=total_turns,
        by_model=[
            {
                "model": model,
                "calls": calls,
                "tokens_in": tokens_in or 0,
                "tokens_out": tokens_out or 0,
                "avg_latency": int(avg_latency or 0),
                "cost": float(cost or 0),
            }
            for model, calls, tokens_in, tokens_out, avg_latency, cost in by_model_rows
        ],
        by_conversation=[
            {
                "conversation_id": str(conversation_id)[:8],
                "turns": turns,
                "cost": float(cost or 0),
                "last": last,
            }
            for conversation_id, turns, cost, last in by_conversation_rows
        ],
    )
    ctx.update(await _meta_costs_context(tenant_id, month_ago))
    return _render("costs.html", ctx)


async def _meta_costs_context(tenant_id: uuid.UUID, since: datetime) -> dict[str, Any]:
    """Costos de WhatsApp (Meta) -- `cost` viene SIEMPRE de `pricing_daily`
    (autoritativo, sincronizado desde `pricing_analytics`); nunca se
    calcula localmente con una tabla de tarifas propia."""
    async with get_session() as session:
        wabas = (
            await session.execute(select(WabaAccount).where(WabaAccount.tenant_id == tenant_id))
        ).scalars().all()
        waba_ids = [w.id for w in wabas]

        by_category_country_rows = []
        by_phone_rows = []
        webhook_volume_rows = []
        pricing_volume_rows = []
        if waba_ids:
            by_category_country_rows = (
                await session.execute(
                    select(
                        PricingDaily.pricing_category, PricingDaily.country,
                        func.sum(PricingDaily.volume), func.sum(PricingDaily.cost),
                    )
                    .where(
                        PricingDaily.waba_account_id.in_(waba_ids), PricingDaily.tenant_id == tenant_id,
                        PricingDaily.date >= since.date(),
                    )
                    .group_by(PricingDaily.pricing_category, PricingDaily.country)
                    .order_by(func.sum(PricingDaily.cost).desc())
                )
            ).all()
            by_phone_rows = (
                await session.execute(
                    select(PricingDaily.phone_number, func.sum(PricingDaily.volume), func.sum(PricingDaily.cost))
                    .where(
                        PricingDaily.waba_account_id.in_(waba_ids), PricingDaily.tenant_id == tenant_id,
                        PricingDaily.date >= since.date(),
                    )
                    .group_by(PricingDaily.phone_number)
                    .order_by(func.sum(PricingDaily.cost).desc())
                )
            ).all()
            # Diferencia de VOLUMEN (nunca de dólares -- el webhook no trae
            # dólares): cuántos eventos facturables vio el webhook por
            # categoría vs. el `volume` que reportó pricing_analytics para
            # esa misma categoría/rango. Sirve para detectar desajustes
            # pronto, no para reemplazar el costo real.
            webhook_volume_rows = (
                await session.execute(
                    select(MessageCost.pricing_category, func.count(MessageCost.id))
                    .where(
                        MessageCost.tenant_id == tenant_id, MessageCost.billable.is_(True),
                        MessageCost.event_ts >= since,
                    )
                    .group_by(MessageCost.pricing_category)
                )
            ).all()
            pricing_volume_rows = (
                await session.execute(
                    select(PricingDaily.pricing_category, func.sum(PricingDaily.volume))
                    .where(
                        PricingDaily.waba_account_id.in_(waba_ids), PricingDaily.tenant_id == tenant_id,
                        PricingDaily.date >= since.date(),
                    )
                    .group_by(PricingDaily.pricing_category)
                )
            ).all()

    webhook_by_cat = {cat: count for cat, count in webhook_volume_rows}
    pricing_by_cat = {cat: int(vol or 0) for cat, vol in pricing_volume_rows}
    all_cats = set(webhook_by_cat) | set(pricing_by_cat)
    discrepancy = [
        {
            "category": cat or "(sin categoría)",
            "webhook_count": webhook_by_cat.get(cat, 0),
            "pricing_volume": pricing_by_cat.get(cat, 0),
            "diff": webhook_by_cat.get(cat, 0) - pricing_by_cat.get(cat, 0),
        }
        for cat in sorted(all_cats, key=lambda c: c or "")
    ]

    return {
        "meta_wabas": wabas,
        "meta_by_category_country": [
            {"category": cat or "(sin categoría)", "country": country or "(sin país)", "volume": int(vol or 0), "cost": float(cost or 0)}
            for cat, country, vol, cost in by_category_country_rows
        ],
        "meta_by_phone": [
            {"phone_number": phone or "(sin número)", "volume": int(vol or 0), "cost": float(cost or 0)}
            for phone, vol, cost in by_phone_rows
        ],
        "meta_discrepancy": discrepancy,
        "meta_service_allowance_hypothesis_enabled": get_settings().meta_service_message_allowance_hypothesis_enabled,
    }


@router.post("/costos/meta/sync")
async def costs_meta_sync(request: Request):
    """Sync manual de `pricing_analytics` para las WABAs de este tenant --
    el mismo trabajo que hace el cron diario (`core.meta_pricing_sync`),
    disparado a demanda."""
    from core.gateway import meta_token_store
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.gateway.meta_pricing_analytics import sync_pricing_daily
    from core.tasks.meta_pricing_sync import _SYNC_WINDOW_DAYS

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    url = "/admin/costos"
    now = datetime.now(UTC)
    start_unix = int((now - timedelta(days=_SYNC_WINDOW_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    end_unix = int(now.timestamp())

    async with get_session() as session:
        rows = (
            await session.execute(
                select(WabaAccount, MetaConnection)
                .join(MetaConnection, MetaConnection.id == WabaAccount.connection_id)
                .where(WabaAccount.tenant_id == tid, MetaConnection.status == "connected")
            )
        ).all()

    errors = []
    for waba, connection in rows:
        token = await meta_token_store.get_token(
            connection.id, TOKEN_KIND, connection.business_id or str(connection.id), tenant_id=tid
        )
        app_secret = await meta_token_store.get_token(connection.id, "app_access", connection.meta_app_id, tenant_id=tid)
        if not token or not app_secret:
            continue
        try:
            await sync_pricing_daily(
                waba.waba_id, waba.id, tid, start_unix, end_unix, token, app_secret,
                connection.graph_api_version, connection.id,
            )
        except GraphError as exc:
            errors.append(exc.message[:100])

    if errors:
        return RedirectResponse(f"{url}?flash={'; '.join(errors)[:200]}&flash_type=danger", status_code=303)
    return RedirectResponse(f"{url}?flash=Costos de Meta sincronizados", status_code=303)


@router.get("/costos/meta/creditos", response_class=HTMLResponse)
async def costs_meta_creditos_page(request: Request):
    ctx = await _base_context(request, "costs")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    async with get_session() as session:
        connections = (
            await session.execute(
                select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.business_id.is_not(None))
            )
        ).scalars().all()

    ctx.update(connections=connections)
    return _render("costs_meta_creditos.html", ctx)


@router.post("/costos/meta/creditos/{connection_id}/verificar", response_class=HTMLResponse)
async def costs_meta_creditos_verificar(request: Request, connection_id: str):
    """Solo lectura (`GET /{business_id}/extendedcredits`) -- Meta no
    soporta crear/actualizar/borrar líneas de crédito. No se le atribuye
    a esto ningún significado de "método de pago configurado: sí/no"."""
    from core.gateway import meta_token_store
    from core.gateway.meta_business_credits import get_extended_credits
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import TOKEN_KIND

    ctx = await _base_context(request, "costs")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    async with get_session() as session:
        connection = await get_owned(session, MetaConnection, uuid.UUID(connection_id), tid)
        connections = (
            await session.execute(
                select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.business_id.is_not(None))
            )
        ).scalars().all()

    credits_error = None
    credits = []
    if connection is None or not connection.business_id:
        credits_error = "Esta conexión no tiene business_id configurado"
    else:
        token = await meta_token_store.get_token(
            connection.id, TOKEN_KIND, connection.business_id, tenant_id=tid
        )
        app_secret = await meta_token_store.get_token(connection.id, "app_access", connection.meta_app_id, tenant_id=tid)
        if not token or not app_secret:
            credits_error = "No hay token disponible para esta conexión"
        else:
            try:
                credits = await get_extended_credits(
                    connection.business_id, token, app_secret, connection.graph_api_version, connection.id
                )
            except GraphError as exc:
                credits_error = exc.message

    ctx.update(connections=connections, credits=credits, credits_error=credits_error, checked_connection_id=connection_id)
    return _render("costs_meta_creditos.html", ctx)


# ------------------------------------------------------------------ plan / billing
@router.get("/plan", response_class=HTMLResponse)
async def plan_page(request: Request):
    """Plan, consumo y suscripción del tenant actual (ADR-0045)."""
    ctx = await _base_context(request, "plan")
    if ctx is None:
        return _login_redirect()
    tenant_id = get_current_tenant_id()
    overview, usage, payments = await _billing_snapshot(tenant_id)
    ctx.update(
        overview=overview,
        usage=usage,
        payments=payments,
        plans=_renderable_plans(overview.plan_id),
    )
    return _render("plan.html", ctx)


async def _billing_snapshot(tenant_id):
    """(overview, usage, payments) del tenant, sin romper si falta el schema."""
    from core.admin.plan_view import (
        billing_payments,
        billing_usage,
        subscription_overview_or_none,
    )

    if tenant_id is None:
        return None, [], []
    overview = await subscription_overview_or_none(tenant_id)
    usage = await billing_usage(tenant_id, overview)
    payments = await billing_payments(tenant_id)
    return overview, usage, payments


def _renderable_plans(current_plan_id: str | None) -> list[dict]:
    from core.billing.plans import public_plans

    result = []
    for plan in public_plans():
        result.append(
            {
                "id": plan.id,
                "name": plan.name,
                "price_cop": plan.price_cop,
                "is_free": plan.is_free,
                "current": plan.id == current_plan_id,
                "limits": plan.limits,
                "features": plan.features,
            }
        )
    return result


@router.post("/plan/subscribe")
async def plan_subscribe(request: Request, plan_id: str = Form(...)):
    """Inicia la suscripción a un plan pago: crea el link de pago del proveedor."""
    if await current_admin(request) is None:
        return _login_redirect()
    from core.billing.checkout import start_subscription

    try:
        checkout_url = await start_subscription(plan_id)
    except Exception as exc:
        log.exception("plan_subscribe_failed", plan=plan_id)
        return RedirectResponse(
            f"/admin/plan?flash=No se pudo iniciar el pago: {str(exc)[:140]}&flash_type=danger",
            status_code=303,
        )
    if checkout_url is None:
        return RedirectResponse(
            "/admin/plan?flash=La pasarela de pago no está configurada&flash_type=danger",
            status_code=303,
        )
    return RedirectResponse(checkout_url, status_code=303)


# ------------------------------------------------------------------ configuración
@router.get("/configuracion", response_class=HTMLResponse)
async def settings_page(request: Request):
    ctx = await _base_context(request, "settings")
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == get_current_tenant_id(),
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if app_settings is None:
        async with get_session() as session:
            app_settings = AppSettings(tenant_id=get_current_tenant_id())
            session.add(app_settings)
    spec = await load_active_spec()
    enabled = enabled_packs(spec)
    all_guardrails = [
        {"name": g.name, "plugin_id": g.plugin_id}
        for g in mcp_host.get_guardrails()
        if g.plugin_id in enabled
    ]
    ctx.update(
        tab=request.query_params.get("tab", "identidad"),
        s=app_settings,
        behavior=dict(app_settings.behavior_config),
        all_guardrails=all_guardrails,
        escalation_keywords=list((app_settings.escalation_config or {}).get("keywords", [])),
        payments_enabled="pack_payments" in enabled,
        # Cerebro / memoria / habilidades (ADR-0050): lo versionable del spec.
        spec=spec.model_dump(mode="json"),
        all_tools=[
            {"name": t.name, "plugin_id": t.plugin_id, "description": t.description}
            for t in mcp_host.get_tools()
            if t.plugin_id in enabled or t.plugin_id == "core"
        ],
    )
    return _render("settings.html", ctx)


@router.post("/configuracion/cerebro")
async def settings_brain_save(
    request: Request,
    model_primary: str = Form(default=""),
    model_fallback: str = Form(default=""),
    model_router: str = Form(default=""),
    model_temperature: float = Form(default=0.3),
    policy_max_tool_calls: int = Form(default=10),
    policy_budget: float = Form(default=0.5),
):
    """Modelo del agente (ADR-0050): primario, fallback, razonamiento y temperatura."""
    if await current_admin(request) is None:
        return _login_redirect()
    current = await load_active_spec()
    data = current.model_dump(mode="json")
    if model_primary:
        data["model"]["primary"] = model_primary.strip()
    data["model"]["fallback"] = model_fallback.strip() or None
    data["model"]["router"] = model_router.strip() or None
    data["model"]["temperature"] = max(0.0, min(2.0, model_temperature))
    data["policies"]["maxToolCallsPerTurn"] = max(1, min(50, policy_max_tool_calls))
    data["policies"]["budgetUsdPerConversation"] = max(0.0, policy_budget)
    return await _save_spec_from_form(request, data, "cerebro")


@router.post("/configuracion/memoria")
async def settings_memory_save(
    request: Request,
    memory_short_term_turns: int = Form(default=30),
    rag_top_k: int = Form(default=0),
    rag_min_score: float = Form(default=0.6),
):
    """Memoria del agente (ADR-0050): turnos de contexto y recall RAG."""
    if await current_admin(request) is None:
        return _login_redirect()
    current = await load_active_spec()
    data = current.model_dump(mode="json")
    data["memory"]["shortTermTurns"] = max(1, min(200, memory_short_term_turns))
    if rag_top_k and rag_top_k > 0:
        data["memory"]["rag"] = {"topK": max(1, min(20, rag_top_k)), "minScore": max(0.0, min(1.0, rag_min_score))}
    else:
        data["memory"]["rag"] = None
    return await _save_spec_from_form(request, data, "memoria")


@router.post("/configuracion/habilidades")
async def settings_skills_save(request: Request):
    """Tools on/off + límites por tool (ADR-0050): edita `spec.tools`."""
    if await current_admin(request) is None:
        return _login_redirect()
    form = await request.form()
    current = await load_active_spec()
    data = current.model_dump(mode="json")
    enabled = enabled_packs(current)

    # Solo se editan tools de packs HABILITADOS (mismo criterio que el GET): así
    # no se pisan los bindings de packs deshabilitados ni se persiste basura.
    editable = {t.name for t in mcp_host.get_tools() if t.plugin_id in enabled}
    bindings_by_tool = {b["tool"]: dict(b) for b in data.get("tools", [])}
    for tool_name in editable:
        max_calls_raw = str(form.get(f"tool_maxcalls:{tool_name}", "") or "").strip()
        try:
            max_calls = int(max_calls_raw)
        except ValueError:
            max_calls = 0
        binding = bindings_by_tool.get(tool_name, {"tool": tool_name, "scopes": []})
        binding["enabled"] = form.get(f"tool_enabled:{tool_name}") is not None
        binding["maxCallsPerConversation"] = max_calls if max_calls > 0 else None
        binding["requiresConfirmation"] = form.get(f"tool_requires:{tool_name}") is not None
        bindings_by_tool[tool_name] = binding
    data["tools"] = list(bindings_by_tool.values())
    return await _save_spec_from_form(request, data, "habilidades")


async def _save_spec_from_form(request: Request, data: dict, tab: str) -> RedirectResponse:
    """Valida y guarda una versión nueva del spec; redirige a la tab de origen."""
    try:
        new_spec = AgentSpec.model_validate(data)
    except Exception as exc:
        return RedirectResponse(
            f"/admin/configuracion?flash=spec_invalido: {str(exc)[:140]}&flash_type=danger",
            status_code=303,
        )
    version = await _save_spec_version(new_spec)
    if version is None:
        return RedirectResponse(
            "/admin/configuracion?flash=hubo una edición concurrente, reintenta&flash_type=danger",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/configuracion?flash={tab} guardado (v{version})&flash_type=success",
        status_code=303,
    )


@router.post("/configuracion/asistente")
async def settings_save(
    request: Request,
    assistant_name: str = Form(...),
    business_description: str = Form(default=""),
    brand_voice: str = Form(default=""),
    tono: str = Form(default="calido"),
    debounce_mode: str = Form(default="natural"),
    guardrail_max_retries: int = Form(default=1),
    disabled_guardrails: list[str] = Form(default=[]),  # noqa: B008
    difficulty_long_message_threshold: int = Form(default=LONG_MESSAGE_THRESHOLD),
    difficulty_turnos_sin_resolver_threshold: int = Form(default=TURNOS_SIN_RESOLVER_THRESHOLD),
    difficulty_score_threshold: int = Form(default=DIFFICULTY_SCORE_THRESHOLD),
    escalation_keywords: str = Form(default=""),
):
    if await current_admin(request) is None:
        return _login_redirect()
    # Validar contra los guardrails REALES de packs que este tenant tiene
    # habilitado -- sin esto, `behavior_config` acumula nombres arbitrarios
    # con el tiempo (hallazgo de `auditor-multitenant`). No es un riesgo de
    # seguridad (el filtro solo puede restringir, nunca ampliar acceso),
    # pero evita basura persistida.
    spec = await load_active_spec()
    valid_names = {g.name for g in mcp_host.get_guardrails() if g.plugin_id in enabled_packs(spec)}
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == get_current_tenant_id(),
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=get_current_tenant_id())
            session.add(app_settings)
        app_settings.assistant_name = assistant_name
        app_settings.business_description = business_description
        app_settings.brand_voice = brand_voice
        behavior = dict(app_settings.behavior_config)
        behavior.update(
            tono=tono,
            debounce_mode=debounce_mode,
            guardrail_max_retries=max(0, guardrail_max_retries),
            disabled_guardrails=sorted(valid_names & set(disabled_guardrails)),
            difficulty_long_message_threshold=max(50, min(5000, difficulty_long_message_threshold)),
            difficulty_turnos_sin_resolver_threshold=max(
                1, min(20, difficulty_turnos_sin_resolver_threshold)
            ),
            difficulty_score_threshold=max(1, min(10, difficulty_score_threshold)),
        )
        app_settings.behavior_config = behavior
        keywords = [t.strip() for t in escalation_keywords.split(",") if t.strip()]
        app_settings.escalation_config = {
            **dict(app_settings.escalation_config or {}),
            "keywords": keywords,
        }
    return RedirectResponse("/admin/configuracion?flash=Configuración guardada", status_code=303)


@router.post("/configuracion/toggle-bot")
async def toggle_global_bot(request: Request):
    if await current_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == get_current_tenant_id(),
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is not None:
            app_settings.bot_paused = not app_settings.bot_paused
    return RedirectResponse("/admin/configuracion?flash=Estado del bot actualizado", status_code=303)


@router.post("/configuracion/llm")
async def settings_llm_save(
    request: Request,
    llm_provider: str = Form(default=""),
    llm_model: str = Form(default=""),
    llm_api_key: str = Form(default=""),
):
    if await current_admin(request) is None:
        return _login_redirect()
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == get_current_tenant_id(),
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=get_current_tenant_id())
            session.add(app_settings)
        app_settings.llm_provider = llm_provider or None
        app_settings.llm_model = llm_model or None
        if llm_api_key:
            app_settings.llm_api_key_encrypted = get_secret_box().encrypt(llm_api_key)
    return RedirectResponse("/admin/configuracion?flash=LLM actualizado", status_code=303)


# ------------------------------------------------------------------ DLQ (reintento manual)
@router.get("/dlq", response_class=HTMLResponse)
async def dlq_page(request: Request):
    """Lista la dead-letter del stream de entrada (mensajes que fallaron N veces)."""
    ctx = await _base_context(request, "dlq")
    if ctx is None:
        return _login_redirect()
    from core.memory.working import get_redis

    r = get_redis()
    my_tenant = str(get_current_tenant_id())
    entries = await r.xrange(DLQ_STREAM, count=200)
    ctx["dlq_entries"] = [
        {
            "entry_id": eid,
            "fields": dict(fields),
            "content": dict(fields).get("content", "")[:120],
        }
        for eid, fields in entries
        if dict(fields).get("tenant_id") == my_tenant
    ]
    return _render("dlq.html", ctx)


@router.post("/dlq/{entry_id}/retry")
async def dlq_retry(request: Request, entry_id: str):
    """Re-encola un evento de la DLQ: revoca el claim de idempotencia (soft-delete)
    y lo devuelve al stream de entrada (ADR-0013). Solo el tenant dueño puede reintentarlo."""
    if await current_admin(request) is None:
        return _login_redirect()
    from sqlalchemy import update

    from core.db.models import ProcessedEvent
    from core.memory.working import get_redis

    r = get_redis()
    rows = await r.xrange(DLQ_STREAM, min=entry_id, max=entry_id)
    if not rows:
        return RedirectResponse("/admin/dlq?flash=El evento ya no está en la DLQ&flash_type=danger", status_code=303)
    _eid, fields = rows[0]
    fields = dict(fields)
    tenant_id = fields.get("tenant_id")

    if tenant_id != str(get_current_tenant_id()):
        return RedirectResponse("/admin/dlq?flash=No puedes reintentar eventos de otro tenant&flash_type=danger", status_code=303)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await session.execute(
                update(ProcessedEvent)
                .where(
                    ProcessedEvent.tenant_id == get_current_tenant_id(),
                    ProcessedEvent.external_message_id == fields.get("external_message_id", ""),
                    ProcessedEvent.deleted_at.is_(None),
                )
                .values(deleted_at=utcnow())
            )

    await r.xadd(INBOUND_STREAM, fields)
    await r.xdel(DLQ_STREAM, entry_id)
    return RedirectResponse("/admin/dlq?flash=Evento reintentado", status_code=303)
