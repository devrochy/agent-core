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
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from core.admin.advanced_service import (
    list_spec_versions,
    save_llm,
)
from core.admin.auth import (
    SESSION_COOKIE,
    TENANT_COOKIE,
    current_admin,
    login,
    logout,
)
from core.admin.collaborators_service import (
    apply_pack_roles,
    create_collaborator,
    ensure_tenant_link,
    grant_permission,
    link_tenant,
    pack_collaborator_roles,
    reset_password,
    resolve_tenant_id,
    revoke_permission,
    target_allowed_for_owner,
    toggle_active,
)
from core.admin.context import base_context as _base_context
from core.admin.context import config_tabs
from core.admin.form_generator import (
    parse_form_values,
    parse_json_schema_form,
    render_form_fields,
    render_json_schema_form,
)
from core.admin.meta_costs_service import (
    list_credit_connections,
    meta_costs_context,
    sync_meta_pricing,
    verify_credits,
)
from core.admin.meta_health_service import (
    diagnose_connection,
    resubscribe_waba,
    set_billing_status,
    set_connection_status,
    soft_delete_connection,
)
from core.admin.meta_numbers_service import (
    add_number,
    list_numbers,
    request_code,
    resolve_waba_and_credentials,
    verify_number,
)
from core.admin.meta_templates_service import (
    delete_template,
    list_templates,
    sync_templates_now,
)
from core.admin.permissions import OWNER_GRANTABLE_SECTIONS
from core.admin.permissions import catalog as sections_catalog
from core.admin.settings_service import (
    format_business_ranges,
    parse_business_ranges,
    save_memory,
)
from core.admin.view_registry import view_registry
from core.admin.waweb_service import (
    accept_risk as waweb_accept_risk,
)
from core.admin.waweb_service import (
    reset_session as waweb_reset_session,
)
from core.admin.waweb_service import (
    session_state as waweb_session_state,
)
from core.agents.spec import (
    CORE_PACK_ID,
    activate_spec_version,
    enabled_packs,
    load_active_spec,
    next_spec_version,
    save_spec_if_changed,
    save_spec_version,
)
from core.billing.enforcement import QuotaExceeded
from core.config import get_settings
from core.db.models import (
    Admin,
    AdminPermission,
    AppSettings,
    Conversation,
    Customer,
    Escalation,
    Message,
    MessageTemplate,
    MetaConnection,
    PhoneNumber,
    PluginConfig,
    PluginRecord,
    PluginTenantState,
    Tenant,
    TenantUser,
    UsageLedger,
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
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# Respuestas que pueden transportar secretos (p.ej. el token extendido del
# wizard): nunca cachear en navegador/proxies.
_NO_STORE = {"Cache-Control": "no-store"}


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def _render(name: str, ctx: dict[str, Any], status_code: int = 200):  # type: ignore[no-untyped-def]
    """Starlette >= 0.29: TemplateResponse(request, name, context)."""
    return templates.TemplateResponse(ctx["request"], name, ctx, status_code=status_code)


# ------------------------------------------------------------------ leads globales
@router.get("/leads", response_class=HTMLResponse)
async def leads_global(request: Request) -> HTMLResponse:
    """Vista global de leads (ADR-0056): `Customer` + columnas que aportan los packs.

    Un lead es un `Customer` (ADR-0022). Cada pack habilitado para el tenant
    registra un `LeadProvider` (columnas + resolver) en `lead_registry`; aquí se
    combinan. El core NO importa packs (invariante nº5).
    """
    from sqlalchemy import select as _select

    from core.admin.lead_registry import lead_registry
    from core.admin.permissions import require_section
    from core.agents.spec import enabled_packs, load_active_spec
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    ctx = await _base_context(request, "leads")
    if ctx is None:
        return _login_redirect()
    if not await require_section(request, "leads"):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)

    tid = get_current_tenant_id()
    async with get_session() as session:
        customers = (
            await session.execute(
                _select(Customer)
                .where(
                    Customer.tenant_id == tid,
                    Customer.deleted_at.is_(None),
                    # El cliente sintético del playground no es un lead del negocio.
                    Customer.channel != TEST_CHANNEL,
                )
                .order_by(Customer.created_at.desc())
                .limit(500)
            )
        ).scalars().all()

    try:
        enabled = enabled_packs(await load_active_spec())
    except Exception:
        enabled = set()
    providers = lead_registry.providers_for(enabled)
    pack_values: dict[Any, dict[str, Any]] = {}
    for provider in providers:
        if provider.resolver is None:
            continue
        try:
            result = await provider.resolver([c.id for c in customers])
        except Exception:
            continue
        for cid, values in (result or {}).items():
            pack_values.setdefault(cid, {}).update(values)

    columns = [
        {"key": "cliente", "label": "Cliente"},
        {"key": "canal", "label": "Canal"},
        {"key": "alta", "label": "Alta"},
    ] + [{"key": col.key, "label": col.label} for provider in providers for col in provider.columns]

    rows = []
    for c in customers:
        row: dict[str, Any] = {
            "cliente": c.name or c.external_id,
            "canal": c.channel,
            "alta": c.created_at.strftime("%Y-%m-%d") if c.created_at else "—",
            "search": f"{c.name or ''} {c.external_id} {c.channel}",
        }
        row.update(pack_values.get(c.id, {}))
        rows.append(row)

    ctx.update(columns=columns, rows=rows, has_packs=bool(providers))
    return _render("leads.html", ctx)


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
        from core.alta_negocio import onboard_tenant

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
            f"/admin/configuracion?flash=tenant {tenant_slug} creado{extra}&flash_type=success", status_code=303
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

    Exige el tenant suspendido y que `confirm_phrase` sea exactamente "ELIMINAR <slug>".
    Solo master.
    """
    if not await _require_master(request):
        return RedirectResponse("/admin?flash=requiere_admin_master&flash_type=danger", status_code=303)
    from urllib.parse import quote

    from core.tenants import TenantNotSuspendedError, purge_tenant

    ref = str(tenant_id)
    try:
        await purge_tenant(ref, confirm_phrase)
    except TenantNotSuspendedError as exc:
        return RedirectResponse(
            f"/admin/tenants?flash={quote(str(exc))}&flash_type=danger", status_code=303
        )
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
    """Delegación a `collaborators_service.resolve_tenant_id` (fuente única)."""
    return await resolve_tenant_id(slug)


async def _ensure_tenant_link(admin_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> None:
    """Delegación a `collaborators_service.ensure_tenant_link` (fuente única)."""
    await ensure_tenant_link(admin_id, tenant_id, role)


async def _pack_collaborator_roles() -> list[dict[str, str]]:
    """Delegación a `collaborators_service.pack_collaborator_roles` (fuente única)."""
    return await pack_collaborator_roles()


async def _apply_pack_roles(values: list[str], tenant_id: uuid.UUID | None, admin_id: uuid.UUID | None) -> None:
    """Delegación a `collaborators_service.apply_pack_roles` (fuente única)."""
    await apply_pack_roles(values, tenant_id, admin_id)


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
    ctx["pack_roles"] = await _pack_collaborator_roles()
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
    pack_roles: list[str] = Form(default=[]),  # noqa: B008
):
    if await current_admin(request) is None:
        return _login_redirect()
    me = await current_admin(request)
    if not await _require_collaborators(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    resultado = await create_collaborator(
        actor=me,
        email=email,
        password=password,
        role=role,
        username=username,
        tenant_slug=tenant,
        pack_roles=pack_roles,
    )
    if isinstance(resultado, str):
        return _collab_redirect(resultado, "danger")
    _audit("create_collaborator", me.email if me else "?", email, role=role, tenant=tenant or "*")
    if resultado.estado == "invitado":
        return _collab_redirect(f"invitación enviada a {email}")
    return _collab_redirect(f"colaborador {email} creado")


async def _target_allowed_for_owner(admin_id: uuid.UUID, tid: uuid.UUID | None) -> bool:
    """Delegación a `collaborators_service.target_allowed_for_owner` (fuente única)."""
    return await target_allowed_for_owner(admin_id, tid)


@router.post("/colaboradores/{admin_id}/password")
async def collaborators_password(
    request: Request, admin_id: uuid.UUID, password: str = Form(...)
):
    if await current_admin(request) is None:
        return _login_redirect()
    me = await current_admin(request)
    if not await _require_collaborators(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    error = await reset_password(actor=me, admin_id=admin_id, password=password)
    if error is not None:
        return _collab_redirect(error, "danger")
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
    error = await grant_permission(
        actor=me, admin_id=admin_id, section=section, access=access, tenant_slug=tenant
    )
    if error is not None:
        return _collab_redirect(error, "danger")
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
    error = await revoke_permission(
        actor=me, admin_id=admin_id, section=section, tenant_slug=tenant
    )
    if error is not None:
        return _collab_redirect(error, "danger")
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
    error = await link_tenant(
        actor=me, admin_id=admin_id, tenant_slug=tenant, role=role
    )
    if error is not None:
        return _collab_redirect(error, "danger")
    _audit("link", me.email, str(admin_id), tenant=tenant, role=role)
    return _collab_redirect(f"vinculado a {tenant} ({role})")


@router.post("/colaboradores/{admin_id}/toggle")
async def collaborators_toggle(request: Request, admin_id: uuid.UUID):
    if await current_admin(request) is None:
        return _login_redirect()
    me = await current_admin(request)
    if not await _require_collaborators(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    error = await toggle_active(
        actor=me, admin_id=admin_id, current_tenant_id=get_current_tenant_id()
    )
    if error is not None:
        return _collab_redirect(error, "danger")
    async with get_session() as session:
        state = "activado" if (await session.get(Admin, admin_id)).active else "desactivado"
    _audit("toggle", me.email if me else "?", str(admin_id))
    return _collab_redirect(f"usuario {state}")


# ------------------------------------------------------------------ AgentSpec
async def _spec_versions() -> list[dict[str, Any]]:
    """Delegación a `advanced_service.list_spec_versions` (fuente única)."""
    return await list_spec_versions(get_current_tenant_id())


def _format_business_ranges(escalation_config: dict) -> str:
    """Delegación a `settings_service.format_business_ranges` (fuente única)."""
    return format_business_ranges(escalation_config)


def _parse_business_ranges(text: str) -> list[dict[str, Any]]:
    """Delegación a `settings_service.parse_business_ranges` (fuente única)."""
    return parse_business_ranges(text)


def _declarative_tools_json(spec) -> str:  # type: ignore[no-untyped-def]
    """Definiciones declarativas del tenant a JSON editable (ADR-0052). Los headers sensibles
    literales salen enmascarados: el panel nunca muestra un secreto (HU-AOS-12)."""
    import json as _json

    from core.agents.declarativas import enmascarar

    tools = enmascarar((spec.extensions or {}).get("tools") or [])
    return _json.dumps(tools, indent=2, ensure_ascii=False) if tools else ""


async def _recent_eval_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Últimas corridas de evals del tenant (ADR-0051)."""
    from core.db.models import EvalRun

    async with get_session() as session:
        rows = (
            await session.execute(
                select(EvalRun)
                .where(
                    EvalRun.tenant_id == get_current_tenant_id(),
                    EvalRun.deleted_at.is_(None),
                )
                .order_by(EvalRun.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [
        {
            "spec_version": r.spec_version or "—",
            "git_ref": r.git_ref or "—",
            "passed": r.passed,
            "total": r.total,
            "created_at": r.created_at,
        }
        for r in rows
    ]


async def _next_spec_version() -> str:
    """Delegación a `core.agents.spec.next_spec_version` (fuente única)."""
    tid = get_current_tenant_id()
    if tid is None:
        return "1.0.0"
    return await next_spec_version(tid)


@router.get("/spec")
async def spec_editor() -> RedirectResponse:
    """El editor de AgentSpec vive ahora en la tab "Avanzado" de Configuración
    (ADR-0050). Se conserva la ruta por compatibilidad con enlaces/docs."""
    return RedirectResponse("/admin/configuracion?tab=avanzado", status_code=307)


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


async def _save_spec_version(new_spec: AgentSpec) -> str | None:
    """Delegación a `core.agents.spec.save_spec_version` (fuente única)."""
    return await save_spec_version(new_spec)


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
            f"/admin/configuracion?tab=avanzado&flash=spec_invalido: {exc}&flash_type=danger", status_code=303
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
            "/admin/configuracion?tab=avanzado&flash=hubo una edición concurrente, reintenta&flash_type=danger",
            status_code=303,
        )
    if activation_errors:
        return RedirectResponse(
            f"/admin/configuracion?tab=avanzado&flash=spec guardado (v{version}), pero no se pudo activar: "
            f"{'; '.join(activation_errors)}&flash_type=danger",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/configuracion?tab=avanzado&flash=spec guardado y activado (v{version})&flash_type=success",
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
                    Conversation.channel != TEST_CHANNEL,
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
                .where(
                    Conversation.tenant_id == my_tenant,
                    Conversation.channel != TEST_CHANNEL,
                )
                .order_by(Conversation.started_at.desc())
                .limit(8)
            )
        ).all()

    settings = get_settings()
    llm_provider = (app_settings.llm_provider if app_settings else None) or settings.llm_provider
    llm_model = (app_settings.llm_model if app_settings else None) or settings.llm_model

    # Packs de negocio habilitados del tenant (sin el core): mismo valor que la
    # métrica `plugins` del plan y que la lista `/admin/plugins` (ADR-0045).
    from core.agents.spec import count_tenant_packs

    tenant_packs = count_tenant_packs(await load_active_spec())

    ctx.update(
        kpi={
            "conversations_7d": conversations_7d,
            "messages_7d": messages_7d,
            "pending_escalations": pending,
            "cost_30d": float(cost_30d),
            "active_plugins": tenant_packs,
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
                .where(Conversation.channel != TEST_CHANNEL)
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
        if conversation.channel == TEST_CHANNEL:
            # Las pruebas del playground no son conversaciones del negocio.
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
                    select(PluginTenantState).where(
                        # Salud por tenant (ADR-0024): solo se muestra como error si
                        # la fila está ACTIVA (deleted_at IS NULL). Una fila con
                        # deleted_at = reactivada/recuperada conserva el error viejo
                        # y NO debe pintarse como falla en el panel.
                        PluginTenantState.tenant_id == tenant_id,
                        PluginTenantState.deleted_at.is_(None),
                    )
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

    # Configurarlo implica que este tenant lo quiere usar.
    try:
        values = parse_form_values(module.config_schema, form_data, previous)
        await configure_plugin_for_tenant(plugin_id, values)
    except QuotaExceeded as exc:
        return _quota_redirect(exc)
    except Exception as exc:
        log.exception("plugin_config_save_failed", plugin=plugin_id)
        return RedirectResponse(
            f"/admin/plugins?flash=Error: {str(exc)[:120]}&flash_type=danger", status_code=303
        )
    return RedirectResponse("/admin/plugins?flash=Plugin configurado y activado", status_code=303)


def _quota_redirect(exc: QuotaExceeded) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/plugins?flash=Tu plan no permite habilitar más plugins "
        f"({exc.used}/{exc.limit}). Mejorá tu plan en /admin/plan.&flash_type=danger",
        status_code=303,
    )


async def configure_plugin_for_tenant(plugin_id: str, values: dict[str, Any]) -> None:
    """Guarda la config del plugin, lo activa y lo habilita para el tenant actual.

    Punto ÚNICO compartido por el panel SSR y la API `/api/v1` (ADR-0076/0077): la
    lógica de cifrado, activación y cuota del pack no se duplica. Levanta
    `QuotaExceeded` si el plan no permite habilitar otro pack.
    """
    await plugin_manager.save_config(plugin_id, values)
    await plugin_manager.activate(plugin_id)
    await _enable_pack_for_tenant(plugin_id)


async def _enable_pack_for_tenant(pack: str) -> int | None:
    """Agrega `pack` a `AgentSpec.packs` del tenant actual (nueva versión).

    Aplica la cuota de plugins del plan (ADR-0045): si ya está en el límite y el
    pack no estaba habilitado, levanta `QuotaExceeded`.
    """
    spec = await load_active_spec()
    if pack in spec.packs:
        return None
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        from core.agents.spec import count_tenant_packs
        from core.billing.enforcement import quota_decision

        decision = await quota_decision(tenant_id, "plugins")
        if not decision.allowed:
            raise QuotaExceeded(
                "plugins", count_tenant_packs(spec), decision.limit or 0
            )
    new_spec = spec.model_copy(update={"packs": sorted({*spec.packs, pack})})
    version = await _save_spec_version(new_spec)
    await _seed_flow_for_current_tenant(pack)
    return version


async def _seed_flow_for_current_tenant(pack: str) -> None:
    """Siembra/actualiza el flujo del pack en el `flow.json` del tenant (ADR-0054).

    Best-effort: un fallo de filesystem no puede tumbar la activación (el runtime
    cae al default del pack si el archivo no está). No pisa las secciones propias
    del tenant ni las de otros packs.
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return
    try:
        from core.agents.flow import seed_tenant_flow
        from core.shared.tenant import tenant_slug

        slug = await tenant_slug(tenant_id)
        if slug:
            seed_tenant_flow(slug, [pack])
    except Exception:
        log.warning("tenant_flow_seed_failed", plugin=pack, exc_info=True)


async def _disable_pack_for_tenant(pack: str) -> int | None:
    """Quita `pack` de `AgentSpec.packs` del tenant actual (nueva versión).

    NO desactiva el código globalmente — otro tenant puede seguir usándolo.
    """
    spec = await load_active_spec()
    if pack not in spec.packs:
        return None
    new_spec = spec.model_copy(update={"packs": sorted(set(spec.packs) - {pack})})
    return await _save_spec_version(new_spec)


async def set_pack_for_tenant(pack: str, enabled: bool) -> str | None:
    """Habilita/deshabilita un pack para el tenant actual (fuente única SSR + API).

    Devuelve un mensaje de error, o `None` si quedó aplicado. Levanta
    `QuotaExceeded` si el plan no permite habilitar otro pack.
    """
    if enabled:
        error = await _ensure_pack_activated(pack)
        if error is not None:
            return error
        await _enable_pack_for_tenant(pack)
        return None
    await _disable_pack_for_tenant(pack)
    return None


@router.post("/plugins/{plugin_id}/activar")
async def plugin_activate(request: Request, plugin_id: str):
    """Habilita `plugin_id` para el TENANT ACTUAL únicamente (AgentSpec.packs)."""
    if await current_admin(request) is None:
        return _login_redirect()
    try:
        error = await set_pack_for_tenant(plugin_id, True)
    except QuotaExceeded as exc:
        return _quota_redirect(exc)
    if error is not None:
        return RedirectResponse(f"/admin/plugins?flash=Error: {error}&flash_type=danger", status_code=303)
    return RedirectResponse("/admin/plugins?flash=Plugin activado para este tenant", status_code=303)


@router.post("/plugins/{plugin_id}/desactivar")
async def plugin_deactivate(request: Request, plugin_id: str):
    """Deshabilita `plugin_id` para el TENANT ACTUAL únicamente. El código sigue
    cargado en el servidor por si otro tenant lo usa."""
    if await current_admin(request) is None:
        return _login_redirect()
    await set_pack_for_tenant(plugin_id, False)
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

    # Selector "Agregar conexión" generado desde el catálogo (ADR-0069): agrupado
    # por categoría, con los no disponibles como "Próximamente".
    from core.gateway.connection_types import (
        SETUP_MANUAL,
        get_connection_type,
        setup_url,
        types_by_category,
    )

    def _edit_url(connection: Any) -> str | None:
        ctype = get_connection_type(connection.type_id or "")
        if ctype is None or ctype.setup_kind != SETUP_MANUAL:
            return None
        return f"/admin/conexiones/nuevo?type={ctype.id}&connection_id={connection.id}"

    catalog = [
        {
            "category": group["category"],
            "label": group["label"],
            "items": [
                {
                    "id": t.id,
                    "label": t.label,
                    "description": t.description,
                    "icon": t.icon,
                    "available": t.available,
                    "url": setup_url(t),
                }
                for t in group["items"]
            ],
        }
        for group in types_by_category()
    ]

    # URL del webhook por tenant (cada app tiene el suyo: /webhooks/whatsapp/<slug>).
    from core.db.models import Tenant
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
            "edit_url": _edit_url(c),
        }
        for c in connections
    ]

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

        from core.gateway.meta_credentials import origin_label

        for mc in meta_connections:
            origen = origin_label(mc.origin)  # del origen guardado (ADR-0094)
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
                # Coexistencia (ADR-0030): el número sigue en la app de WhatsApp
                # Business y en Cloud API a la vez. No requiere migración: se lee
                # de `health` (lo setea `meta_onboarding.persist_finish_payload`).
                "coexistencia": bool((mc.health or {}).get("coexistence")),
            })

    rows.sort(key=lambda r: r["created_at"] or utcnow(), reverse=True)

    ctx.update(
        connections=rows,
        has_connection=bool(rows),
        webhook_url=tenant_webhook_url(tenant_slug) if tenant_slug else "",
        catalog=catalog,
    )
    return _render("conexiones.html", ctx)


@router.get("/conexiones/waweb", response_class=HTMLResponse)
async def conexiones_waweb(request: Request):
    """Vínculo por QR de un número propio — canal NO oficial (ADR-0074)."""
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()
    tid = get_current_tenant_id()
    state = await waweb_session_state(tid)
    ctx.update(waweb={**state, "qr_uri": state.get("qr_image")})
    return _render("conexiones_waweb.html", ctx)


@router.post("/conexiones/waweb/aceptar-riesgo")
async def conexiones_waweb_aceptar_riesgo(request: Request):
    admin = await current_admin(request)
    if admin is None:
        return _login_redirect()
    error = await waweb_accept_risk(get_current_tenant_id(), admin.id)
    if error is not None:
        log.warning("waweb_accept_risk_failed", error=error)
        return RedirectResponse(
            "/admin/conexiones/waweb?flash=No se pudo registrar la aceptación&flash_type=danger",
            status_code=303,
        )
    return RedirectResponse(
        "/admin/conexiones/waweb?flash=Aviso aceptado — esperando el QR&flash_type=success",
        status_code=303,
    )


@router.post("/conexiones/waweb/reiniciar")
async def conexiones_waweb_reiniciar(request: Request):
    if await current_admin(request) is None:
        return _login_redirect()
    error = await waweb_reset_session(get_current_tenant_id())
    if error is not None:
        log.warning("waweb_reset_failed", error=error)
    return RedirectResponse(
        "/admin/conexiones/waweb?flash=Vínculo reiniciado&flash_type=success", status_code=303
    )


@router.get("/conexiones/waweb/qr.json")
async def conexiones_waweb_qr(request: Request):
    """Estado + QR vigente (data URI). Lo consulta la vista por polling."""
    if await current_admin(request) is None:
        return _login_redirect()
    return await waweb_session_state(get_current_tenant_id())


@router.get("/conexiones/nuevo", response_class=HTMLResponse)
async def connection_new(request: Request):
    """Formulario genérico de credenciales para tipos `manual` (alta o edición)."""
    if await current_admin(request) is None:
        return _login_redirect()
    from core.admin.form_generator import render_json_schema_form
    from core.gateway.connection_types import SETUP_MANUAL, get_connection_type

    type_id = request.query_params.get("type", "")
    connection_id = request.query_params.get("connection_id", "")
    current_values: dict[str, Any] = {}
    label_value = ""
    connection_uuid: uuid.UUID | None = None
    if connection_id:
        try:
            connection_uuid = uuid.UUID(connection_id)
        except (ValueError, TypeError):
            connection_uuid = None
        if connection_uuid is None:
            return RedirectResponse(
                "/admin/conexiones?flash=Conexión inválida&flash_type=danger", status_code=303
            )
        from core.db.models import ChannelConnection
        from core.db.session import get_owned, get_session
        from core.gateway.connections import credentials_of

        async with get_session() as session:
            connection = await get_owned(
                session, ChannelConnection, connection_uuid, get_current_tenant_id()
            )
        if connection is None:
            return RedirectResponse(
                "/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303
            )
        current_values = credentials_of(connection)
        label_value = connection.label or ""
        type_id = connection.type_id or type_id

    ctype = get_connection_type(type_id)
    if ctype is None or not ctype.available or ctype.setup_kind != SETUP_MANUAL:
        return RedirectResponse(
            "/admin/conexiones?flash=Tipo de conexión inválido&flash_type=danger",
            status_code=303,
        )

    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()
    ctx.update(
        ctype_id=ctype.id,
        ctype_label=ctype.label,
        ctype_description=ctype.description,
        form_fields=render_json_schema_form(ctype.credentials_schema, current_values),
        connection_id=str(connection_uuid) if connection_uuid else "",
        label_value=label_value,
    )
    return _render("conexion_form.html", ctx)


@router.post("/conexiones/nuevo")
async def connection_new_save(request: Request):
    """Guarda (alta o edición) una conexión de un tipo `manual`."""
    if await current_admin(request) is None:
        return _login_redirect()
    from core.admin.form_generator import parse_json_schema_form
    from core.gateway.connection_types import SETUP_MANUAL, get_connection_type

    form = await request.form()
    type_id = str(form.get("type_id", ""))
    label = str(form.get("label", "") or "")
    connection_id = str(form.get("connection_id", "") or "")
    ctype = get_connection_type(type_id)
    if ctype is None or not ctype.available or ctype.setup_kind != SETUP_MANUAL:
        return RedirectResponse(
            "/admin/conexiones?flash=Tipo de conexión inválido&flash_type=danger",
            status_code=303,
        )

    # Edición: cargar credenciales previas para conservar los secretos en blanco.
    connection_uuid: uuid.UUID | None = None
    previous: dict[str, Any] = {}
    if connection_id:
        try:
            connection_uuid = uuid.UUID(connection_id)
        except (ValueError, TypeError):
            connection_uuid = None
        if connection_uuid is None:
            return RedirectResponse(
                "/admin/conexiones?flash=Conexión inválida&flash_type=danger", status_code=303
            )
        from core.db.models import ChannelConnection
        from core.db.session import get_owned, get_session
        from core.gateway.connections import credentials_of

        async with get_session() as session:
            existing = await get_owned(
                session, ChannelConnection, connection_uuid, get_current_tenant_id()
            )
        if existing is None:
            return RedirectResponse(
                "/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303
            )
        previous = credentials_of(existing)

    form_data = {str(k): str(v) for k, v in form.items()}
    credentials = parse_json_schema_form(ctype.credentials_schema, form_data, previous)
    missing = [k for k in ctype.credentials_schema.get("required", []) if not credentials.get(k)]
    if missing:
        suffix = f"&connection_id={connection_id}" if connection_id else ""
        return RedirectResponse(
            f"/admin/conexiones/nuevo?type={ctype.id}{suffix}"
            f"&flash=Faltan campos: {', '.join(missing)}&flash_type=danger",
            status_code=303,
        )

    try:
        from core.gateway.connections import save_connection

        _connection, created = await save_connection(
            label or ctype.label, credentials, connection_uuid, type_id=ctype.id
        )
        log.info(
            "connection_type_saved",
            tenant_id=str(get_current_tenant_id() or ""),
            type_id=ctype.id,
            created=created,
            connection_id=str(_connection.id),
        )
        action = "creada" if created else "actualizada"
        return RedirectResponse(f"/admin/conexiones?flash=Conexión {ctype.label} {action} ✓", status_code=303)
    except Exception as exc:
        log.exception("connection_type_save_failed")
        suffix = f"&connection_id={connection_id}" if connection_id else ""
        return RedirectResponse(
            f"/admin/conexiones/nuevo?type={ctype.id}{suffix}&flash=Error al guardar: {str(exc)[:120]}&flash_type=danger",
            status_code=303,
        )


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

# ------------------------------------------------------------------ números adicionales de una WABA
# Prefijo "meta/" para no chocar con /conexiones/{connection_id}/... de
# arriba, que son rutas de ChannelConnection (modelo viejo/distinto).
@router.get("/conexiones/meta/{connection_id}/numeros", response_class=HTMLResponse)
async def meta_connection_numeros_page(request: Request, connection_id: str):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, _creds = await resolve_waba_and_credentials(uuid.UUID(connection_id), tid)
    if connection is None:
        return RedirectResponse(
            "/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303
        )

    numbers, sync_error = await list_numbers(tid, connection_id)
    ctx.update(connection=connection, waba=waba, numbers=numbers, sync_error=sync_error)
    return _render("meta_numeros.html", ctx)


@router.post("/conexiones/meta/{connection_id}/numeros/agregar")
async def meta_connection_numeros_agregar(
    request: Request, connection_id: str, cc: str = Form(...), phone_number: str = Form(...), verified_name: str = Form(...)
):
    if await current_admin(request) is None:
        return _login_redirect()

    url = f"/admin/conexiones/meta/{connection_id}/numeros"
    error = await add_number(
        get_current_tenant_id(), connection_id, cc=cc, phone_number=phone_number, verified_name=verified_name
    )
    if error is not None:
        return RedirectResponse(f"{url}?flash={error}&flash_type=danger", status_code=303)
    return RedirectResponse(f"{url}?flash=Número agregado", status_code=303)


@router.post("/conexiones/meta/{connection_id}/numeros/{phone_number_id}/solicitar-codigo")
async def meta_connection_numeros_solicitar_codigo(
    request: Request, connection_id: str, phone_number_id: str, code_method: str = Form(...), language: str = Form(default="es")
):
    if await current_admin(request) is None:
        return _login_redirect()

    url = f"/admin/conexiones/meta/{connection_id}/numeros"
    error = await request_code(
        get_current_tenant_id(), connection_id, phone_number_id, code_method=code_method, language=language
    )
    if error is not None:
        return RedirectResponse(f"{url}?flash={error}&flash_type=danger", status_code=303)
    return RedirectResponse(f"{url}?flash=Código enviado", status_code=303)


@router.post("/conexiones/meta/{connection_id}/numeros/{phone_number_id}/verificar")
async def meta_connection_numeros_verificar(request: Request, connection_id: str, phone_number_id: str, code: str = Form(...)):
    if await current_admin(request) is None:
        return _login_redirect()

    url = f"/admin/conexiones/meta/{connection_id}/numeros"
    ok, error = await verify_number(
        get_current_tenant_id(), connection_id, phone_number_id, code=code
    )
    if error is not None:
        return RedirectResponse(f"{url}?flash={error}&flash_type=danger", status_code=303)
    if not ok:
        return RedirectResponse(f"{url}?flash=Código incorrecto&flash_type=danger", status_code=303)
    return RedirectResponse(f"{url}?flash=Número verificado", status_code=303)


# ---------------------------------------------------------- plantillas de WhatsApp
@router.get("/conexiones/meta/{connection_id}/plantillas", response_class=HTMLResponse)
async def meta_connection_plantillas_page(request: Request, connection_id: str):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, _creds = await resolve_waba_and_credentials(uuid.UUID(connection_id), tid)
    if connection is None:
        return RedirectResponse(
            "/admin/conexiones?flash=Conexión no encontrada&flash_type=danger", status_code=303
        )

    templates, sync_error, at_limit = await list_templates(tid, connection_id)
    ctx.update(connection=connection, waba=waba, templates=templates, sync_error=sync_error, at_limit=at_limit)
    return _render("meta_plantillas.html", ctx)


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


@router.post("/conexiones/meta/{connection_id}/plantillas/{template_id}/editar")
async def meta_connection_plantilla_editar(request: Request, connection_id: str, template_id: str, category: str = Form(None)):
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_templates import TemplateValidationError, update_template

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, _waba, creds = await resolve_waba_and_credentials(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    if connection is None or creds is None:
        return RedirectResponse(f"{url}?flash=No hay conexión lista todavía&flash_type=danger", status_code=303)

    async with get_session() as session:
        row = await get_owned(session, MessageTemplate, uuid.UUID(template_id), tid)
    if row is None or not row.meta_template_id:
        return RedirectResponse(f"{url}?flash=Plantilla no encontrada&flash_type=danger", status_code=303)

    try:
        await update_template(
            row.meta_template_id, creds.access_token, creds.app_secret, creds.graph_api_version,
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
    if await current_admin(request) is None:
        return _login_redirect()

    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    error = await delete_template(get_current_tenant_id(), connection_id, template_id)
    if error is not None:
        return RedirectResponse(f"{url}?flash={error}&flash_type=danger", status_code=303)
    return RedirectResponse(f"{url}?flash=Plantilla eliminada (todos los idiomas)", status_code=303)


@router.post("/conexiones/meta/{connection_id}/plantillas/{template_id}/eliminar-traduccion")
async def meta_connection_plantilla_eliminar_traduccion(request: Request, connection_id: str, template_id: str):
    """Borra SOLO esta versión de idioma."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_templates import delete_template_one_language

    if await current_admin(request) is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connection, waba, creds = await resolve_waba_and_credentials(uuid.UUID(connection_id), tid)
    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    if connection is None or waba is None or creds is None:
        return RedirectResponse(f"{url}?flash=No hay conexión/WABA lista todavía&flash_type=danger", status_code=303)

    async with get_session() as session:
        row = await get_owned(session, MessageTemplate, uuid.UUID(template_id), tid)
    if row is None or not row.meta_template_id:
        return RedirectResponse(f"{url}?flash=Plantilla no encontrada&flash_type=danger", status_code=303)

    try:
        await delete_template_one_language(
            waba.waba_id, row.name, row.meta_template_id, creds.access_token, creds.app_secret,
            creds.graph_api_version, connection.id, tid,
        )
    except GraphError as exc:
        return RedirectResponse(f"{url}?flash={exc.message[:200]}&flash_type=danger", status_code=303)

    return RedirectResponse(f"{url}?flash=Traducción eliminada ({row.language})", status_code=303)


@router.post("/conexiones/meta/{connection_id}/plantillas/sync")
async def meta_connection_plantillas_sync(request: Request, connection_id: str):
    if await current_admin(request) is None:
        return _login_redirect()

    url = f"/admin/conexiones/meta/{connection_id}/plantillas"
    error = await sync_templates_now(get_current_tenant_id(), connection_id)
    if error is not None:
        return RedirectResponse(f"{url}?flash={error}&flash_type=danger", status_code=303)
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

    # Expiración del access token por conexión (badge de aviso antes de que venza): la
    # del token que la conexión usa según su `origin` (ADR-0094).
    from core.gateway.meta_credentials import access_token_expires_at

    token_expiry: dict = {}
    for connection in connections:
        token_expiry[connection.id] = await access_token_expires_at(connection)

    ctx.update(connections=connections, token_expiry=token_expiry, now=datetime.now(UTC))
    return _render("meta_salud.html", ctx)


@router.post("/conexiones/meta/{connection_id}/activar")
async def meta_connection_activar(request: Request, connection_id: str):
    """Delegación a `meta_health_service` (fuente única)."""
    if await current_admin(request) is None:
        return _login_redirect()
    error = await set_connection_status(
        get_current_tenant_id(), connection_id, active=True
    )
    if error is not None:
        return RedirectResponse(f"/admin/conexiones?flash={error}&flash_type=danger", status_code=303)
    return RedirectResponse("/admin/conexiones?flash=Conexión activada", status_code=303)



@router.post("/conexiones/meta/{connection_id}/desactivar")
async def meta_connection_desactivar(request: Request, connection_id: str):
    """Delegación a `meta_health_service` (fuente única)."""
    if await current_admin(request) is None:
        return _login_redirect()
    error = await set_connection_status(
        get_current_tenant_id(), connection_id, active=False
    )
    if error is not None:
        return RedirectResponse(f"/admin/conexiones?flash={error}&flash_type=danger", status_code=303)
    return RedirectResponse("/admin/conexiones?flash=Conexión desactivada", status_code=303)



@router.post("/conexiones/meta/{connection_id}/eliminar")
async def meta_connection_eliminar(request: Request, connection_id: str):
    """Delegación a `meta_health_service` (fuente única)."""
    if await current_admin(request) is None:
        return _login_redirect()
    error = await soft_delete_connection(get_current_tenant_id(), connection_id)
    if error is not None:
        return RedirectResponse(f"/admin/conexiones?flash={error}&flash_type=danger", status_code=303)
    return RedirectResponse("/admin/conexiones?flash=Conexión eliminada", status_code=303)



@router.post("/conexiones/salud/{connection_id}/diagnosticar")
async def meta_health_diagnosticar(request: Request, connection_id: str):
    """Diagnóstico on-demand -- corre los 7 checks síncrono (debe responder
    en <10s para 1 WABA + 2 números, criterio de aceptación del ticket)."""
    if await current_admin(request) is None:
        return _login_redirect()

    error = await diagnose_connection(get_current_tenant_id(), connection_id)
    if error is not None:
        return RedirectResponse(
            f"/admin/conexiones/salud?flash={error}&flash_type=danger", status_code=303
        )
    return RedirectResponse("/admin/conexiones/salud?flash=Diagnóstico completo", status_code=303)


@router.post("/conexiones/salud/{connection_id}/resubscribe-waba")
async def meta_health_resubscribe_waba(request: Request, connection_id: str):
    """Reparación INLINE del check `webhook_subscribed` -- reencola SOLO
    la suscripción de la WABA, nunca el wizard de OAuth completo."""
    if await current_admin(request) is None:
        return _login_redirect()

    error = await resubscribe_waba(get_current_tenant_id(), connection_id)
    if error is not None:
        return RedirectResponse(
            f"/admin/conexiones/salud?flash={error}&flash_type=danger", status_code=303
        )
    return RedirectResponse("/admin/conexiones/salud?flash=WABA re-suscripta", status_code=303)


@router.post("/conexiones/salud/{connection_id}/billing")
async def meta_health_set_billing_status(request: Request, connection_id: str, manual_status: str = Form(...)):
    """El único "repair" que no llama a Meta -- billing no tiene API, esto
    solo persiste lo que un admin marcó a mano después de configurarlo en
    WhatsApp Manager (ver /admin/costos/meta/creditos)."""
    if await current_admin(request) is None:
        return _login_redirect()

    error = await set_billing_status(get_current_tenant_id(), connection_id, manual_status)
    if error is not None:
        return RedirectResponse(
            f"/admin/conexiones/salud?flash={error}&flash_type=danger", status_code=303
        )
    return RedirectResponse(
        "/admin/conexiones/salud?flash=Estado de facturación actualizado", status_code=303
    )



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
    """Delegación a `meta_costs_service.meta_costs_context` (fuente única)."""
    return await meta_costs_context(tenant_id, since)


@router.post("/costos/meta/sync")
async def costs_meta_sync(request: Request):
    """Sync manual de `pricing_analytics` (delegado al servicio compartido)."""
    if await current_admin(request) is None:
        return _login_redirect()

    errors = await sync_meta_pricing(get_current_tenant_id())
    if errors:
        return RedirectResponse(
            f"/admin/costos?flash={'; '.join(errors)[:200]}&flash_type=danger", status_code=303
        )
    return RedirectResponse("/admin/costos?flash=Costos de Meta sincronizados", status_code=303)


@router.get("/costos/meta/creditos", response_class=HTMLResponse)
async def costs_meta_creditos_page(request: Request):
    ctx = await _base_context(request, "costs")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connections = await list_credit_connections(tid)

    ctx.update(connections=connections)
    return _render("costs_meta_creditos.html", ctx)


@router.post("/costos/meta/creditos/{connection_id}/verificar", response_class=HTMLResponse)
async def costs_meta_creditos_verificar(request: Request, connection_id: str):
    """Solo lectura (`GET /{business_id}/extendedcredits`) -- Meta no
    soporta crear/actualizar/borrar líneas de crédito. No se le atribuye
    a esto ningún significado de "método de pago configurado: sí/no"."""
    ctx = await _base_context(request, "costs")
    if ctx is None:
        return _login_redirect()

    tid = get_current_tenant_id()
    connections = await list_credit_connections(tid)
    credits, credits_error = await verify_credits(tid, connection_id)

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
    from core.orchestrator.verificador import TOGGLES, normalizar_desactivados

    all_guardrails = [
        {"name": g.name, "plugin_id": g.plugin_id}
        for g in mcp_host.get_guardrails()
        if g.plugin_id in enabled
    ] + [{"name": name, "plugin_id": "core"} for name in TOGGLES]
    # Flujo de atención (ADR-0054): solo lectura en el panel (la edición es por
    # archivo del tenant, ADR-0017/0026). `resolve_flow` cae al default del pack
    # si el tenant todavía no tiene flow.json. Se filtra por las tools realmente
    # habilitadas para el tenant, mismo criterio que el runtime.
    from core.agents.flow import resolve_flow
    from core.mcp.declarative import tools_for_spec

    available_tool_names = {
        t.name for t in mcp_host.get_tools() if t.plugin_id in enabled or t.plugin_id == "core"
    } | {t.name for t in tools_for_spec(spec)}
    resolved_flow = await resolve_flow(spec, available=available_tool_names)
    ctx.update(
        tab=request.query_params.get("tab", "identidad"),
        s=app_settings,
        behavior={
            **dict(app_settings.behavior_config),
            "disabled_guardrails": normalizar_desactivados(
                (app_settings.behavior_config or {}).get("disabled_guardrails", [])
            ),
        },
        all_guardrails=all_guardrails,
        escalation_keywords=list((app_settings.escalation_config or {}).get("keywords", [])),
        escalation=dict(app_settings.escalation_config or {}),
        escalation_ranges_text=_format_business_ranges(app_settings.escalation_config or {}),
        payments_enabled="pack_payments" in enabled,
        # Cerebro / memoria / habilidades (ADR-0050): lo versionable del spec.
        spec=spec.model_dump(mode="json"),
        versions=await _spec_versions(),
        guardrails_text=_format_custom_guardrails((spec.extensions or {}).get("guardrails") or []),
        declarative_tools_json=_declarative_tools_json(spec),
        eval_runs=await _recent_eval_runs(),
        config_tabs=await config_tabs(request),
        all_tools=[
            {"name": t.name, "plugin_id": t.plugin_id, "description": t.description}
            for t in mcp_host.get_tools()
            if t.plugin_id in enabled or t.plugin_id == "core"
        ],
        flow_source=resolved_flow.source if resolved_flow else "",
        flow_sections=[
            {"label": s.label, "purpose": s.purpose, "tools": s.tools}
            for s in (resolved_flow.sections if resolved_flow else [])
        ],
        flow_unknown=resolved_flow.unknown_tools if resolved_flow else [],
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
    await save_memory(
        get_current_tenant_id(),
        short_term_turns=memory_short_term_turns,
        rag_top_k=rag_top_k,
        rag_min_score=rag_min_score,
    )
    return RedirectResponse("/admin/configuracion?tab=memoria&flash=memoria guardada", status_code=303)


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
    # Guardrails propios del tenant (ADR-0051): DSL seguro en extensions["guardrails"].
    if form.get("guardrails_text") is not None:
        from core.orchestrator.guardrails import validate_custom_rules

        extensions = dict(data.get("extensions") or {})
        extensions["guardrails"] = validate_custom_rules(
            _parse_custom_guardrails(str(form.get("guardrails_text") or ""))
        )
        data["extensions"] = extensions
    # Tools declarativas del tenant (ADR-0052): JSON en extensions["tools"].
    if form.get("declarative_tools_json") is not None:
        import json as _json
        from urllib.parse import quote

        from core.agents.declarativas import preparar
        from core.agents.secretos import guardar_secretos_declarativos, secretos_configurados
        from core.kernel.procedures import catalogo_de_acciones
        from core.mcp.declarative import rejected_domains
        from core.plugin_manager import plugin_manager

        extensions = dict(data.get("extensions") or {})
        raw = str(form.get("declarative_tools_json") or "").strip()
        parsed: object = []
        if raw:
            try:
                parsed = _json.loads(raw)
            except _json.JSONDecodeError as exc:
                return RedirectResponse(
                    f"/admin/configuracion?tab=habilidades&flash=JSON de tools inválido: {str(exc)[:120]}&flash_type=danger",
                    status_code=303,
                )
        rejected = rejected_domains(parsed)
        if rejected:
            # Anti-SSRF (ADR-0052): solo dominios públicos con punto; ni IPs ni
            # nombres internos (`redis`, `localhost`, `*.internal`).
            msg = f"allowed_domains no permitidos (usa dominios públicos): {', '.join(rejected)[:160]}"
            return RedirectResponse(
                f"/admin/configuracion?tab=habilidades&flash={quote(msg)}&flash_type=danger",
                status_code=303,
            )
        # Mismo validador que `/api/v1` (HU-AOS-12): sin literales en headers sensibles (los
        # enmascarados se migran a la instalación `core`). Los secretos nuevos se cargan desde
        # el panel nuevo: el SSR no tiene campo para valores.
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return _login_redirect()
        preparadas = preparar(
            parsed,
            extensions.get("tools"),
            {},
            await secretos_configurados(tenant_id),
            set(catalogo_de_acciones(plugin_manager.list_modules())),
        )
        if preparadas.errores:
            ruta, msg = next(iter(preparadas.errores.items()))
            return RedirectResponse(
                f"/admin/configuracion?tab=habilidades&flash={quote(f'{ruta}: {msg}'[:200])}&flash_type=danger",
                status_code=303,
            )
        if preparadas.secretos:
            await guardar_secretos_declarativos(tenant_id, preparadas.secretos)
        extensions["tools"] = preparadas.tools
        data["extensions"] = extensions
    return await _save_spec_from_form(request, data, "habilidades")


def _parse_custom_guardrails(text: str) -> list[dict[str, Any]]:
    """Parsea guardrails propios, una regla por línea:

    `forbidden_phrase | frase1, frase2 | retry|escalate`
    `required_tool | frase1, frase2 | tool_name | retry|escalate`
    `max_length | 500 | retry|escalate`
    """
    rules: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        kind = parts[0]
        if kind == "max_length":
            try:
                value = int(parts[1])
            except ValueError:
                continue
            action = parts[2] if len(parts) > 2 else "retry"
            rules.append({"kind": kind, "value": value, "action": action})
        elif kind == "required_tool":
            if len(parts) < 3:
                continue
            phrases = [p.strip() for p in parts[1].split(",") if p.strip()]
            action = parts[3] if len(parts) > 3 else "retry"
            rules.append(
                {"kind": kind, "phrases": phrases, "tool": parts[2], "action": action}
            )
        elif kind == "forbidden_phrase":
            phrases = [p.strip() for p in parts[1].split(",") if p.strip()]
            action = parts[2] if len(parts) > 2 else "retry"
            rules.append({"kind": kind, "phrases": phrases, "action": action})
    return rules


def _format_custom_guardrails(rules: list[dict[str, Any]]) -> str:
    lines = []
    for r in rules or []:
        kind = r.get("kind")
        if kind == "max_length":
            lines.append(f"max_length | {r.get('value')} | {r.get('action', 'retry')}")
        elif kind == "required_tool":
            phrases = ", ".join(r.get("phrases") or [])
            lines.append(f"required_tool | {phrases} | {r.get('tool')} | {r.get('action', 'retry')}")
        elif kind == "forbidden_phrase":
            phrases = ", ".join(r.get("phrases") or [])
            lines.append(f"forbidden_phrase | {phrases} | {r.get('action', 'retry')}")
    return "\n".join(lines)


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


async def _save_spec_if_changed(current: AgentSpec, data: dict) -> str | None:
    """Delegación a `core.agents.spec.save_spec_if_changed` (fuente única)."""
    return await save_spec_if_changed(current, data)


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
    business_tz: str = Form(default=""),
    business_ranges: str = Form(default=""),
    out_of_hours_message: str = Form(default=""),
    auto_escalate_after_turns: int = Form(default=0),
    persona_tone: str = Form(default=""),
    persona_warmth: int = Form(default=3),
    persona_emojis: str = Form(default="sparse"),
    persona_max_length: int = Form(default=500),
    persona_forbidden: str = Form(default=""),
):
    if await current_admin(request) is None:
        return _login_redirect()
    # Validar contra los guardrails REALES de packs que este tenant tiene
    # habilitado -- sin esto, `behavior_config` acumula nombres arbitrarios
    # con el tiempo (hallazgo de `auditor-multitenant`). No es un riesgo de
    # seguridad (el filtro solo puede restringir, nunca ampliar acceso),
    # pero evita basura persistida.
    spec = await load_active_spec()
    from core.orchestrator.verificador import TOGGLES, normalizar_desactivados

    valid_names = {g.name for g in mcp_host.get_guardrails() if g.plugin_id in enabled_packs(spec)} | set(TOGGLES)
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
            disabled_guardrails=sorted(valid_names & set(normalizar_desactivados(disabled_guardrails))),
            difficulty_long_message_threshold=max(50, min(5000, difficulty_long_message_threshold)),
            difficulty_turnos_sin_resolver_threshold=max(
                1, min(20, difficulty_turnos_sin_resolver_threshold)
            ),
            difficulty_score_threshold=max(1, min(10, difficulty_score_threshold)),
        )
        app_settings.behavior_config = behavior
        keywords = [t.strip() for t in escalation_keywords.split(",") if t.strip()]
        ranges = _parse_business_ranges(business_ranges)
        business_hours: dict[str, Any] | None = None
        if ranges:
            business_hours = {"tz": business_tz.strip() or "UTC", "ranges": ranges}
        app_settings.escalation_config = {
            **dict(app_settings.escalation_config or {}),
            "keywords": keywords,
            "business_hours": business_hours,
            "out_of_hours_message": out_of_hours_message.strip(),
            "auto_escalate_after_turns": max(0, auto_escalate_after_turns),
        }
    # Personalidad del AgentSpec (ADR-0050): se persiste como versión nueva del
    # spec (versionable), no en AppSettings.
    persona_data = spec.model_dump(mode="json")
    persona_data["persona"].update(
        {
            "tone": persona_tone or persona_data["persona"]["tone"],
            "warmth": max(0, min(5, persona_warmth)),
            "emojis": persona_emojis or persona_data["persona"]["emojis"],
            "maxLength": max(0, persona_max_length),
            "forbiddenPhrases": [p.strip() for p in persona_forbidden.split(",") if p.strip()],
        }
    )
    await _save_spec_if_changed(spec, persona_data)
    return RedirectResponse("/admin/configuracion?flash=Configuración guardada", status_code=303)


@router.post("/configuracion/spec/activar")
async def spec_activate(request: Request, record_id: str = Form(...)):
    """Activa una versión concreta del AgentSpec (rollback, ADR-0051)."""
    me = await current_admin(request)
    if me is None:
        return _login_redirect()
    tid = get_current_tenant_id()
    if tid is None:
        return RedirectResponse(
            "/admin/configuracion?tab=avanzado&flash=sin tenant activo&flash_type=danger",
            status_code=303,
        )
    try:
        version = await activate_spec_version(tid, record_id=uuid.UUID(record_id))
    except ValueError as exc:
        return RedirectResponse(
            f"/admin/configuracion?tab=avanzado&flash=No se pudo activar: {str(exc)[:120]}&flash_type=danger",
            status_code=303,
        )
    _audit("spec_activate", me.email, str(tid), version=version)
    return RedirectResponse(
        f"/admin/configuracion?tab=avanzado&flash=versión {version} activada&flash_type=success",
        status_code=303,
    )


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
    await save_llm(
        get_current_tenant_id(),
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
    )
    return RedirectResponse("/admin/configuracion?flash=LLM actualizado", status_code=303)


# ------------------------------------------------------------------ DLQ (reintento manual)
