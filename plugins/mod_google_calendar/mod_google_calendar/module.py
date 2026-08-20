"""GoogleCalendarModule: BaseModule con rutas admin propias (flujo OAuth2).

Demuestra:
- Config con SecretStr -> formulario auto-generado con input password.
- admin_router: un plugin puede aportar endpoints FastAPI propios.
- admin_menu: entrada en el menú lateral solo mientras esté activo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.mcp.types import BaseModule
from mod_google_calendar.config import CalendarConfig
from mod_google_calendar.skills import CalendarSkill

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

PLUGIN_ID = "mod_google_calendar"

admin_router = APIRouter(prefix=f"/admin/plugins/{PLUGIN_ID}", tags=[PLUGIN_ID])


@admin_router.get("/oauth")
async def start_oauth(request: Request):  # type: ignore[no-untyped-def]
    """Redirige a Google para conectar la cuenta del negocio."""
    from core.admin.auth import current_admin
    from core.plugin_manager import plugin_manager

    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    config = await plugin_manager.load_config(PLUGIN_ID)
    if config is None:
        return RedirectResponse(
            f"/admin/plugins/{PLUGIN_ID}/configurar?flash=Configura primero client_id y secret",
            status_code=303,
        )
    from mod_google_calendar.oauth import build_authorization_url

    return RedirectResponse(build_authorization_url(config.client_id))  # type: ignore[attr-defined]


@admin_router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = ""):  # type: ignore[no-untyped-def]
    from core.admin.auth import current_admin
    from core.plugin_manager import plugin_manager

    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not code:
        return HTMLResponse("<h3>Autorización cancelada.</h3>", status_code=400)

    config = await plugin_manager.load_config(PLUGIN_ID)
    if config is None:
        return RedirectResponse("/admin/plugins", status_code=303)

    from mod_google_calendar.oauth import exchange_code, save_tokens

    tokens = await exchange_code(
        config.client_id,  # type: ignore[attr-defined]
        config.client_secret.get_secret_value(),  # type: ignore[attr-defined]
        code,
    )
    await save_tokens(tokens)
    return RedirectResponse("/admin/plugins?flash=Google Calendar conectado ✓", status_code=303)


class GoogleCalendarModule(BaseModule):
    id = PLUGIN_ID
    name = "Google Calendar"
    version = "1.0.0"
    description = "Agendamiento de citas con OAuth2 de Google"
    requires_config = True
    config_schema = CalendarConfig
    admin_menu = [
        {"label": "Calendar · Conectar", "path": f"/admin/plugins/{PLUGIN_ID}/oauth"},
    ]
    admin_router = admin_router
    declared_skills = [CalendarSkill]

    def on_activate(self, host: "MCPHost") -> None:
        config = self.config
        if not isinstance(config, CalendarConfig):
            from core.shared.exceptions import PluginConfigError

            raise PluginConfigError(PLUGIN_ID, "Config inválida o ausente")
        host.register_skill(CalendarSkill(config))
