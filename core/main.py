"""Entrypoint FastAPI del monolito.

Un solo proceso contiene: gateway (webhooks), orchestrator (consumer + debounce
como tasks de fondo), panel admin y todos los plugins activos (in-process).
Celery worker/beat corren como procesos hermanos (docker-compose).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from core.admin.auth import SESSION_COOKIE, current_admin
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

    # 1. Detectar y registrar plugins (entry_points)
    plugin_manager.scan()
    await plugin_manager.sync_registry()
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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.middleware("http")
async def tenant_context_middleware(request: Request, call_next):
    """Establece el contexto de tenant (ADR-0012) para rutas autenticadas.

    Allowlist de rutas públicas (webhooks, healthz, login, estáticos); para todo
    lo demás, si hay cookie de sesión, resuelve admin -> tenant_user -> tenant_scope.
    Así cubre el panel (/admin), los routers de plugins (/crm, /calendar) y cualquier
    ruta futura sin tener que acordarse de registrar prefijos.
    """
    path = request.url.path
    if path == "/" or path.startswith(("/webhooks", "/healthz", "/admin/login", "/static")):
        return await call_next(request)
    if SESSION_COOKIE not in request.cookies:
        return await call_next(request)
    admin = await current_admin(request)
    if admin is not None:
        tenants = await admin_tenants(admin.id)
        if tenants:
            async with tenant_scope(tenants[0][0]):
                return await call_next(request)
    return await call_next(request)


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/admin")
