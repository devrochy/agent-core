"""Entrypoint FastAPI del monolito.

Un solo proceso contiene: gateway (webhooks), orchestrator (consumer + debounce
como tasks de fondo), panel admin y todos los plugins activos (in-process).
Celery worker/beat corren como procesos hermanos (docker-compose).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from core.admin.views import router as admin_router
from core.config import get_settings
from core.gateway.webhooks import router as webhooks_router
from core.orchestrator.consumer import consume_loop
from core.orchestrator.debounce import poll_debounce_loop, recover_orphaned_conversations
from core.plugin_manager import plugin_manager
from core.shared.logger import configure_logging, get_logger

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
for _module in plugin_manager.list_modules():
    plugin_router = getattr(_module, "admin_router", None)
    if plugin_router is not None:
        app.include_router(plugin_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/admin")
