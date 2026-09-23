"""Entrypoint FastAPI del monolito.

Un solo proceso contiene: gateway (webhooks), orchestrator (consumer + debounce
como tasks de fondo), panel admin y todos los plugins activos (in-process).
Celery worker/beat corren como procesos hermanos (docker-compose).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core.admin.auth import SESSION_COOKIE, TENANT_COOKIE, current_admin
from core.admin.permissions import effective_access, pack_prefixes, required_section
from core.admin.views import router as admin_router
from core.config import get_settings
from core.gateway.webhooks import router as webhooks_router
from core.mcp.host import mcp_host
from core.orchestrator.consumer import consume_loop
from core.orchestrator.debounce import poll_debounce_loop, recover_orphaned_conversations
from core.plugin_manager import plugin_manager
from core.shared.logger import configure_logging, get_logger
from core.shared.tenant import admin_tenants, tenant_scope
from core.tools.core_skill import register_core_skills

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info("starting", app=settings.app_name, env=settings.app_env)

    from core.observability.sentry_setup import configure_sentry

    configure_sentry()  # no-op sin SENTRY_ENABLED/SENTRY_DSN

    # 1. Detectar y registrar plugins (entry_points)
    plugin_manager.scan()
    await plugin_manager.sync_registry()
    # 1b. Espejar el catálogo de planes a la tabla `plans` (ADR-0045)
    from core.billing.plans import sync_plans

    await sync_plans()
    # 2. Activar los que estaban configurados/activos
    await plugin_manager.activate_all_configured()
    # 3. Crash recovery del debounce
    await recover_orphaned_conversations()
    # 4. Tasks de fondo: consumer del stream + poller del debounce
    consumer_task = asyncio.create_task(consume_loop())
    debounce_task = asyncio.create_task(poll_debounce_loop())

    yield

    consumer_task.cancel()
    debounce_task.cancel()
    log.info("shutdown")


app = FastAPI(title="agent-core", lifespan=lifespan, docs_url=None, redoc_url=None)

app.include_router(webhooks_router)
app.include_router(admin_router)

# Estáticos del panel (table.css/table.js del motor data-table)
_admin_static = Path(__file__).resolve().parent / "admin" / "static"
app.mount("/static", StaticFiles(directory=str(_admin_static)), name="static")

# Rutas admin aportadas por plugins (routers propios). El scan aquí es necesario
# porque los routers deben montarse ANTES de arrancar el servidor; el lifespan
# vuelve a escanear (idempotente) para el registro en BD y la activación.
plugin_manager.scan()
# Tools nativas del core (Sprint 2): plomería del framework, registrada como skill "core".
register_core_skills(mcp_host)
for _module in plugin_manager.list_modules():
    plugin_router = getattr(_module, "admin_router", None)
    if plugin_router is not None:
        app.include_router(plugin_router)
    # Routers admin extra (pestañas de Configuración, ADR-0049): por contrato el
    # prefijo debe quedar bajo /admin para que el gate del panel aplique.
    for _extra in _module.extra_admin_routers():
        _prefix = getattr(_extra, "prefix", "")
        if not _prefix.startswith("/admin"):
            log.warning("extra_admin_router_fuera_de_admin", plugin=_module.id, prefix=_prefix)
            continue
        app.include_router(_extra)

# Prefijos de UI admin de los packs (ej. /crm → pack_commerce), para el gate por
# sección del middleware (ADR-0047). Se calcula una vez, tras el scan.
_PACK_PREFIXES = pack_prefixes(plugin_manager.list_modules())


def _forbidden(request: Request):
    """403 del gate de sección; JSON si la ruta lo pide, HTML si no."""
    if request.url.path.endswith(".json") or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"detail": "Sin permiso para esta sección."}, status_code=403)
    return HTMLResponse("Sin permiso para esta sección.", status_code=403)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.middleware("http")
async def tenant_context_middleware(request: Request, call_next):
    """Establece el contexto de tenant (ADR-0012) y aplica el gate de sección (ADR-0047).

    Allowlist de rutas públicas (webhooks, healthz, login, estáticos); para todo
    lo demás, si hay cookie de sesión, resuelve admin -> tenant_user -> tenant_scope
    y, salvo `master`, exige el permiso de la sección de la ruta (read para GET,
    write para el resto). Así cubre el panel (/admin), los routers de plugins
    (/crm, /calendar) y cualquier ruta futura sin tener que acordarse de prefijos.
    """
    path = request.url.path
    if path == "/" or path.startswith(
        (
            "/webhooks",
            "/healthz",
            "/admin/login",
            "/admin/logout",
            "/admin/switch-tenant",
            "/static",
        )
    ):
        return await call_next(request)
    if SESSION_COOKIE not in request.cookies:
        return await call_next(request)
    admin = await current_admin(request)
    if admin is None:
        return await call_next(request)

    tenants = await admin_tenants(admin.id)
    target: uuid.UUID | None = None
    if tenants:
        # Selector de tenant (C3, Sprint 4): la cookie ac_tenant decide el
        # tenant activo del panel; si no es válida o falta, el primero.
        target = tenants[0][0]
        cookie_tenant = request.cookies.get(TENANT_COOKIE)
        if cookie_tenant and any(str(t[0]) == cookie_tenant for t in tenants):
            target = uuid.UUID(cookie_tenant)

    required = required_section(path, request.method, _PACK_PREFIXES)
    async with tenant_scope(target):
        # Default-deny: cualquier ruta autenticada que no esté mapeada a una
        # sección se rechaza (las públicas ya salieron por la allowlist de arriba).
        if required is None:
            return _forbidden(request)
        if admin.role != "master":
            section, write = required
            access = await effective_access(admin, section, target)
            if access is None or (write and access != "write"):
                return _forbidden(request)
        return await call_next(request)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/admin")
