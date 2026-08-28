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
from core.admin.form_generator import parse_form_values, render_form_fields
from core.agents.spec import CORE_PACK_ID, load_active_spec
from core.config import get_settings
from core.db.models import (
    AgentSpecRecord,
    AppSettings,
    Conversation,
    Customer,
    Escalation,
    Message,
    PluginRecord,
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
from core.plugin_manager import plugin_manager
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger
from core.shared.tenant import admin_tenants, get_current_tenant_id, tenant_scope

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

INBOUND_STREAM = "agent_core:inbound"
DLQ_STREAM = "agent_core:inbound:dlq"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def _base_context(request: Request, section: str) -> dict[str, Any] | None:
    """Contexto común. None => redirigir a login."""
    admin = await current_admin(request)
    if admin is None:
        return None
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == get_current_tenant_id(),
                    AppSettings.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    return {
        "request": request,
        "section": section,
        "admin": admin,
        "assistant_name": app_settings.assistant_name if app_settings else get_settings().app_name,
        "plugin_menu": plugin_manager.active_admin_menu(),
        "tenants": await _admin_tenant_options(admin.id),
        "active_tenant_id": str(get_current_tenant_id()) if get_current_tenant_id() else "",
        "flash": request.query_params.get("flash"),
        "flash_type": request.query_params.get("flash_type"),
    }


async def _admin_tenant_options(admin_id) -> list[dict[str, str]]:
    """Opciones del selector de tenant (C3): tenants del admin con nombre y activo."""
    active = str(get_current_tenant_id()) if get_current_tenant_id() else ""
    return [
        {"id": str(tid), "name": tname, "role": trole, "active": str(tid) == active}
        for (tid, tname, trole) in await admin_tenants(admin_id)
    ]


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
    installed = sorted({m.id for m in plugin_manager.list_modules()})
    ctx["spec"] = spec.model_dump(mode="json")
    ctx["installed_packs"] = [p for p in installed if p != CORE_PACK_ID]
    ctx["enabled_packs"] = sorted(set(spec.packs) - {CORE_PACK_ID})
    ctx["versions"] = await _spec_versions()
    return _render("spec.html", ctx)


@router.post("/spec")
async def spec_save(request: Request):
    admin = await current_admin(request)
    if admin is None:
        return _login_redirect()
    form = await request.form()
    packs = [str(x) for x in form.getlist("packs")]
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
    escalation_triggers = str(form.get("escalation_triggers", ""))
    raw_json = str(form.get("raw_json", ""))

    try:
        if raw_json.strip():
            # fallback de poder: JSON completo validado por el contrato congelado
            data = json.loads(raw_json)
            new_spec = AgentSpec.model_validate(data)
        else:
            current = await load_active_spec()
            data = current.model_dump(mode="json")
            data["packs"] = sorted(set(packs))
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
            data["policies"]["escalation"]["triggers"] = [
                t.strip() for t in escalation_triggers.split(",") if t.strip()
            ]
            new_spec = AgentSpec.model_validate(data)
    except Exception as exc:
        return RedirectResponse(
            f"/admin/spec?flash=spec_invalido: {exc}&flash_type=danger", status_code=303
        )

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
        # Raza de dos saves concurrentes: los índices parciales protegieron la
        # integridad (nunca dos activos ni dos versiones iguales); el usuario
        # reintenta.
        return RedirectResponse(
            "/admin/spec?flash=hubo una edición concurrente, reintenta&flash_type=danger",
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
    ctx = await _base_context(request, "plugins")
    if ctx is None:
        return _login_redirect()
    async with get_session() as session:
        records = {r.id: r for r in (await session.execute(select(PluginRecord))).scalars().all()}

    plugins = []
    for module in plugin_manager.list_modules():
        record = records.get(module.id)
        plugins.append(
            {
                "id": module.id,
                "name": module.name or module.id,
                "version": module.version,
                "status": record.status if record else "detected",
                "error_message": record.error_message if record else None,
                "has_config": module.config_schema is not None,
            }
        )
    # Plugins en BD pero no instalados (pip uninstall sin limpiar registro)
    installed_ids = {m.id for m in plugin_manager.list_modules()}
    for record in records.values():
        if record.id not in installed_ids:
            plugins.append(
                {
                    "id": record.id,
                    "name": record.name,
                    "version": record.version,
                    "status": "no instalado",
                    "error_message": "Paquete no encontrado en entry_points",
                    "has_config": False,
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
            from core.db.models import PluginConfig as PluginConfigModel

            stored = (
                await session.execute(
                    select(PluginConfigModel).where(
                        PluginConfigModel.plugin_id == plugin_id,
                        PluginConfigModel.tenant_id == get_current_tenant_id(),
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
        return RedirectResponse("/admin/plugins?flash=Plugin configurado y activado", status_code=303)
    except Exception as exc:
        log.exception("plugin_config_save_failed", plugin=plugin_id)
        return RedirectResponse(
            f"/admin/plugins?flash=Error: {str(exc)[:120]}&flash_type=danger", status_code=303
        )


@router.post("/plugins/{plugin_id}/activar")
async def plugin_activate(request: Request, plugin_id: str):
    if await current_admin(request) is None:
        return _login_redirect()
    try:
        await plugin_manager.activate(plugin_id)
        return RedirectResponse("/admin/plugins?flash=Plugin activado", status_code=303)
    except Exception as exc:
        return RedirectResponse(
            f"/admin/plugins?flash=Error: {str(exc)[:120]}&flash_type=danger", status_code=303
        )


@router.post("/plugins/{plugin_id}/desactivar")
async def plugin_deactivate(request: Request, plugin_id: str):
    if await current_admin(request) is None:
        return _login_redirect()
    await plugin_manager.deactivate(plugin_id)
    return RedirectResponse("/admin/plugins?flash=Plugin desactivado", status_code=303)


# ------------------------------------------------------------------ conexiones
@router.get("/conexiones", response_class=HTMLResponse)
async def connections_page(request: Request):
    ctx = await _base_context(request, "connections")
    if ctx is None:
        return _login_redirect()
    connections = await list_connections()
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
    ok, msg, info = await verify_meta_credentials(phone_number_id, access_token)
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
    ctx.update(s=app_settings, behavior=dict(app_settings.behavior_config))
    return _render("settings.html", ctx)


@router.post("/configuracion/asistente")
async def settings_save(
    request: Request,
    assistant_name: str = Form(...),
    business_description: str = Form(default=""),
    brand_voice: str = Form(default=""),
    tono: str = Form(default="calido"),
    debounce_mode: str = Form(default="natural"),
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
        app_settings.assistant_name = assistant_name
        app_settings.business_description = business_description
        app_settings.brand_voice = brand_voice
        behavior = dict(app_settings.behavior_config)
        behavior.update(tono=tono, debounce_mode=debounce_mode)
        app_settings.behavior_config = behavior
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
