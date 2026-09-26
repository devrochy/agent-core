"""GoogleCalendarModule: pack-calendar v2 (ADR-0069/0070).

- `admin_router` = panel propio `/calendar/*` con navegación por secciones
  (ADR-0061/0063): Conexión · Horarios y calendario · Tipos de cita · Citas.
- El router OAuth conserva su URL `/admin/plugins/pack_calendar/oauth[/callback]`
  (redirect URI registrado en Google Console, ADR-0070), por eso va como
  `extra_admin_routers`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.mcp.types import BaseModule
from core.shared.logger import get_logger
from pack_calendar.admin import router as calendar_admin_router
from pack_calendar.config import CalendarConfig
from pack_calendar.skills import CalendarSkill

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

PLUGIN_ID = "pack_calendar"

log = get_logger(__name__)

# OAuth: conserva la URL vieja (ver docstring).
oauth_router = APIRouter(prefix=f"/admin/plugins/{PLUGIN_ID}", tags=[PLUGIN_ID])


@oauth_router.get("/oauth")
async def start_oauth(request: Request):  # type: ignore[no-untyped-def]
    """Redirige a Google para conectar la cuenta del negocio."""
    from core.admin.auth import current_admin
    from core.admin.permissions import require_section
    from core.plugin_manager import plugin_manager

    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    # Escribe credenciales: exige permiso de escritura (no alcanza con estar logueado).
    if not await require_section(request, "plugins", write=True):
        return RedirectResponse(
            "/admin/conexiones?flash=Sin permiso para conectar cuentas&flash_type=danger",
            status_code=303,
        )
    from core.shared.exceptions import PluginConfigError

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except PluginConfigError:
        config = None
    if config is None:
        return RedirectResponse(
            "/calendar/configuracion?flash=Configurá primero client_id y secret&flash_type=danger",
            status_code=303,
        )
    from pack_calendar.oauth import build_authorization_url

    return RedirectResponse(build_authorization_url(config.client_id))  # type: ignore[attr-defined]


@oauth_router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = ""):  # type: ignore[no-untyped-def]
    from core.admin.auth import current_admin
    from core.admin.permissions import require_section
    from core.plugin_manager import plugin_manager

    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    # El callback guarda credenciales: exige escritura (evita que un colaborador
    # de solo lectura pise la conexión del tenant vía GET).
    if not await require_section(request, "plugins", write=True):
        return HTMLResponse("Sin permiso.", status_code=403)
    if not code:
        return HTMLResponse("<h3>Autorización cancelada.</h3>", status_code=400)

    from core.shared.exceptions import PluginConfigError

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except PluginConfigError:
        config = None
    if config is None:
        return RedirectResponse(
            "/calendar/?flash=El pack no está configurado&flash_type=danger", status_code=303
        )

    from core.shared.exceptions import PluginError
    from pack_calendar.oauth import exchange_code, save_tokens

    try:
        tokens = await exchange_code(
            config.client_id,  # type: ignore[attr-defined]
            config.client_secret.get_secret_value(),  # type: ignore[attr-defined]
            code,
        )
        await save_tokens(tokens)
    except PluginError as exc:
        from urllib.parse import quote

        log.warning("calendar_oauth_callback_failed", error=str(exc)[:200])
        return RedirectResponse(
            f"/calendar/?flash={quote(str(exc)[:140])}&flash_type=danger", status_code=303
        )
    return RedirectResponse("/calendar/?flash=Google Calendar conectado ✓", status_code=303)


class GoogleCalendarModule(BaseModule):
    id = PLUGIN_ID
    name = "Google Calendar"
    version = "2.0.0"
    description = "Agendamiento de citas con OAuth2 de Google"
    requires_config = True
    config_schema = CalendarConfig
    # Submenú al habilitar el pack (ADR-0061/0063).
    admin_menu: list[dict[str, str]] = [
        {"label": "Conexión", "path": "/calendar/", "section": "Configuración", "section_key": "pack_calendar:conexion"},
        {"label": "Horarios y calendario", "path": "/calendar/configuracion", "section": "Configuración", "section_key": "pack_calendar:configuracion"},
        {"label": "Tipos de cita", "path": "/calendar/tipos", "section": "Configuración", "section_key": "pack_calendar:tipos"},
        {"label": "Citas", "path": "/calendar/citas", "section": "Operación", "section_key": "pack_calendar:citas"},
    ]
    admin_router = calendar_admin_router
    declared_skills: list[type] = [CalendarSkill]

    def on_activate(self, host: MCPHost) -> None:
        config = self.config
        if not isinstance(config, CalendarConfig):
            from core.shared.exceptions import PluginConfigError

            raise PluginConfigError(PLUGIN_ID, "Config inválida o ausente")
        host.register_skill(CalendarSkill())

    def extra_admin_routers(self) -> list[APIRouter]:
        """Router OAuth con la URL vieja (redirect URI de Google Console)."""
        return [oauth_router]
