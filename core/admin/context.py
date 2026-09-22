"""Contexto común de las vistas del panel (SSR).

`base_context` construye el dict que toda plantilla heredada de `base.html`
espera (incluido el selector de tenants). Vive aquí —y no en `views.py`— para
que los packs con `admin_router` propio lo reutilicen SIN arrastrar los imports
pesados de las vistas (gateway, agents.spec): los packs dependen del core, no de
sus rutas.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy import select

from core.config import get_settings
from core.db.models import AppSettings
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from core.shared.tenant import admin_tenants, get_current_tenant_id


async def admin_tenant_options(admin_id) -> list[dict[str, str]]:
    """Opciones del selector de tenant (C3): tenants del admin con nombre y activo."""
    active = str(get_current_tenant_id()) if get_current_tenant_id() else ""
    return [
        {"id": str(tid), "name": tname, "role": trole, "active": str(tid) == active}
        for (tid, tname, trole) in await admin_tenants(admin_id)
    ]


async def base_context(request: Request, section: str) -> dict[str, Any] | None:
    """Contexto común. None => redirigir a login."""
    from core.admin.auth import current_admin

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
    # Import local (no a nivel de módulo): los packs con admin_router propio
    # reusan base_context sin necesitar arrastrar agents.spec si no llaman a
    # esta rama. El menú lateral debe reflejar solo los packs de ESTE tenant
    # (AgentSpec.packs), no todo lo activo globalmente en el proceso.
    from core.agents.spec import enabled_packs, load_active_spec

    tenant_enabled = enabled_packs(await load_active_spec())
    from core.admin.plan_view import dashboard_billing_summary

    tenant_id = get_current_tenant_id()
    billing = await dashboard_billing_summary(tenant_id) if tenant_id else None
    return {
        "request": request,
        "section": section,
        "admin": admin,
        "assistant_name": app_settings.assistant_name if app_settings else get_settings().app_name,
        "plugin_menu": plugin_manager.active_admin_menu(tenant_enabled),
        "tenants": await admin_tenant_options(admin.id),
        "active_tenant_id": str(get_current_tenant_id()) if get_current_tenant_id() else "",
        "billing": billing,
        "flash": request.query_params.get("flash"),
        "flash_type": request.query_params.get("flash_type"),
    }