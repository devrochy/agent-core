"""API JSON unificada `/api/v1` (ADR-0076/0077).

Un solo contrato OpenAPI para el panel web y las apps. Los realms de identidad
(admin por token opaco de sesión; gestor por JWT de pack) viven en `deps.py` y se
amplían por slice. El esquema se exporta sin levantar el servidor:

    python scripts/export_openapi.py
"""

from __future__ import annotations

from fastapi import APIRouter

from core.api.routers import (
    auth,
    collaborators,
    connections,
    conversations,
    costs,
    dashboard,
    dlq,
    escalations,
    invitations,
    leads,
    me,
    meta_ops,
    meta_wizard,
    plan,
    platform,
    playground,
    plugins,
    push,
    settings,
    tenants,
    web_chat,
)

api_router = APIRouter(prefix="/api/v1", tags=["api"])
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(push.router)
api_router.include_router(tenants.router)
api_router.include_router(dashboard.router)
api_router.include_router(dlq.router)
api_router.include_router(conversations.router)
api_router.include_router(playground.router)
api_router.include_router(escalations.router)
api_router.include_router(leads.router)
api_router.include_router(plugins.router)
api_router.include_router(connections.router)
api_router.include_router(meta_ops.router)
api_router.include_router(meta_wizard.router)
api_router.include_router(collaborators.router)
api_router.include_router(invitations.router)
api_router.include_router(costs.router)
api_router.include_router(plan.router)
api_router.include_router(platform.router)
api_router.include_router(settings.router)
api_router.include_router(web_chat.router)

__all__ = ["api_router"]