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
from sqlalchemy.exc import IntegrityError

from core.admin.auth import SESSION_COOKIE, TENANT_COOKIE, current_admin, login, logout
from core.admin.context import base_context as _base_context
from core.admin.form_generator import (
    parse_form_values,
    parse_json_schema_form,
    render_form_fields,
    render_json_schema_form,
)
from core.admin.view_registry import view_registry
from core.agents.spec import CORE_PACK_ID, enabled_packs, load_active_spec
from core.config import get_settings
from core.db.models import (
    AgentSpecRecord,
    AppSettings,
    Conversation,
    Customer,
    Escalation,
    Message,
    PluginConfig,
    PluginRecord,
    PluginTenantState,
    UsageLedger,
    utcnow,
)
from core.db.session import get_session
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
    ctx["tenants_rows"] = [
        {
            "id": str(t.id),
            "name": t.name,
            "slug": t.slug,
            "status": t.status,
            "created_at": t.created_at,
        }
        for t in rows
    ]
    return _render("tenants.html", ctx)


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
    wa_label: str = Form(default=""),
    wa_phone_number_id: str = Form(default=""),
    wa_app_secret: str = Form(default=""),
    wa_access_token: str = Form(default=""),
    wa_verify_token: str = Form(default=""),
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

    whatsapp = None
    if connect_whatsapp:
        whatsapp = {
            "label": wa_label or name,
            "phone_number_id": wa_phone_number_id,
            "app_secret": wa_app_secret,
            "access_token": wa_access_token,
            "verify_token": wa_verify_token,
        }
        ok, msg, _info = await verify_meta_credentials(wa_phone_number_id, wa_access_token, wa_app_secret)
        if not ok:
            return RedirectResponse(
                f"/admin/tenants/nuevo?flash=WhatsApp: {msg[:120]}&flash_type=danger", status_code=303
            )

    triggers = [t.strip() for t in escalation_triggers.split(",") if t.strip()]
    try:
        from core.onboarding import onboard_tenant

        tid, _steps = await onboard_tenant(
            name=name,
            slug=tenant_slug,
            business_description=business,
            language=language,
            persona={"role": "asistente", "tone": tone, "locale": f"es-{language.upper()}"},
            escalation_triggers=triggers or None,
            packs=packs or None,
            admin_email=admin.email,
            whatsapp=whatsapp,
        )
    except Exception as exc:
        log.exception("tenant_create_failed")
        return RedirectResponse(
            f"/admin/tenants/nuevo?flash=Error al crear: {str(exc)[:140]}&flash_type=danger",
            status_code=303,
        )

    response = RedirectResponse(
        f"/admin/spec?flash=tenant {tenant_slug} creado&flash_type=success", status_code=303
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
    await _enable_pack_for_tenant(plugin_id)
    return RedirectResponse("/admin/plugins?flash=Plugin configurado y activado", status_code=303)


async def _enable_pack_for_tenant(pack: str) -> int | None:
    """Agrega `pack` a `AgentSpec.packs` del tenant actual (nueva versión)."""
    spec = await load_active_spec()
    if pack in spec.packs:
        return None
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
    await _enable_pack_for_tenant(plugin_id)
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
    from core.db.models import Tenant
    from core.tenants import tenant_webhook_url

    tenant_slug = ""
    tid = get_current_tenant_id()
    if tid is not None:
        async with get_session() as session:
            tenant = await session.get(Tenant, tid)
            tenant_slug = tenant.slug if tenant else ""

    ctx.update(
        connections=[
            {
                "id": c.id,
                "channel": c.channel,
                "provider": c.provider,
                "label": c.label or c.external_id,
                "active": c.active,
                "is_primary": c.is_primary,
                "external_id": c.external_id,
                "created_at": c.created_at,
            }
            for c in connections
        ],
        has_connection=bool(connections),
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
    from core.gateway.connections import effective_credentials
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
    verify_token = (await effective_credentials()).get("verify_token", "") or settings.whatsapp_verify_token

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
    return _render("costs.html", ctx)


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
        s=app_settings,
        behavior=dict(app_settings.behavior_config),
        all_guardrails=all_guardrails,
        escalation_keywords=list((app_settings.escalation_config or {}).get("keywords", [])),
    )
    return _render("settings.html", ctx)


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
