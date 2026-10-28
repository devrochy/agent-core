"""API JSON unificada `/api/v1` (ADR-0076/0077).

Un solo contrato OpenAPI para el panel web y las apps. Los realms de identidad
(admin por token opaco de sesión; gestor por JWT de pack) viven en `deps.py` y se
amplían por slice. El esquema se exporta sin levantar el servidor:

    python scripts/export_openapi.py
"""

from __future__ import annotations

from fastapi import APIRouter

from core.api.routers import (
    a2a,
    access,
    agent_data,
    agent_editor,
    agents,
    audit,
    auth,
    collaborators,
    connection_agents,
    connection_releases,
    connections,
    conversation_tasks,
    conversations,
    costs,
    dashboard,
    dashboard_agents,
    datos_cliente,
    datos_de_prueba,
    declarative_tools,
    dlq,
    domain_events,
    escalations,
    fiabilidad,
    files,
    invitations,
    leads,
    leads_acciones,
    me,
    mensajes,
    meta_instagram,
    meta_mensaje_prueba,
    meta_messenger,
    meta_ops,
    meta_promocion,
    meta_wizard,
    notifications,
    onboarding,
    plan,
    plantillas_opciones,
    platform,
    platform_altas,
    platform_meta_es,
    playground,
    plugins,
    procedures,
    push,
    qa,
    search,
    settings,
    skill_installations,
    sync,
    tenants,
    turnos,
    web_chat,
)

api_router = APIRouter(prefix="/api/v1", tags=["api"])
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(push.router)
api_router.include_router(access.router)
api_router.include_router(notifications.router)
api_router.include_router(search.router)
api_router.include_router(sync.router)
api_router.include_router(audit.router)
api_router.include_router(tenants.router)
api_router.include_router(dashboard.router)
api_router.include_router(dashboard_agents.router)
api_router.include_router(onboarding.router)
api_router.include_router(dlq.router)
api_router.include_router(conversations.router)
api_router.include_router(conversation_tasks.router)
api_router.include_router(playground.router)
api_router.include_router(escalations.router)
api_router.include_router(leads.router)
api_router.include_router(leads_acciones.router)
api_router.include_router(plugins.router)
api_router.include_router(skill_installations.router)
api_router.include_router(procedures.router)
# HU-AOS-47: antes que `agents` (su `/{agent_id}` capturaría `/overview`).
api_router.include_router(agent_data.router)
api_router.include_router(datos_de_prueba.router)
api_router.include_router(datos_de_prueba.modulos_router)
api_router.include_router(agents.router)
api_router.include_router(agent_editor.router)
api_router.include_router(connections.router)
api_router.include_router(connection_agents.router)
api_router.include_router(connection_releases.router)
api_router.include_router(a2a.router)
api_router.include_router(meta_ops.router)
api_router.include_router(meta_wizard.router)
api_router.include_router(meta_messenger.router)
api_router.include_router(meta_instagram.router)
api_router.include_router(meta_promocion.router)
api_router.include_router(meta_mensaje_prueba.router)
api_router.include_router(plantillas_opciones.router)
api_router.include_router(mensajes.router)
api_router.include_router(collaborators.router)
api_router.include_router(invitations.router)
api_router.include_router(files.router)
api_router.include_router(costs.router)
api_router.include_router(fiabilidad.router)
api_router.include_router(turnos.router)
api_router.include_router(plan.router)
api_router.include_router(platform.router)
api_router.include_router(platform_altas.router)
api_router.include_router(platform_meta_es.router)
api_router.include_router(qa.router)
api_router.include_router(declarative_tools.router)
api_router.include_router(settings.router)
api_router.include_router(datos_cliente.router)
api_router.include_router(domain_events.router)
api_router.include_router(web_chat.router)

__all__ = ["api_router"]