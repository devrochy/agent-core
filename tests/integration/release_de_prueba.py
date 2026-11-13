"""Conexión con su release 1 para el negocio de un test (ADR-0191).

Todo turno corre sobre la release v3 de su conexión: un test que arma su negocio y sus
conversaciones a mano las ata a la conexión que monta aquí, con el mismo camino que los journeys
(`evals/journeys.py::encender_runtime_v3`)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

_RAIZ = Path(__file__).resolve().parents[2]


def _journeys() -> Any:
    if "journeys" not in sys.modules:
        spec = importlib.util.spec_from_file_location("journeys", _RAIZ / "evals" / "journeys.py")
        assert spec and spec.loader
        modulo = importlib.util.module_from_spec(spec)
        sys.modules["journeys"] = modulo
        spec.loader.exec_module(modulo)
    return sys.modules["journeys"]


async def montar_release(tenant_id: uuid.UUID) -> uuid.UUID:
    """Conexión conversacional del negocio con su release 1 activa; deja el negocio en v3."""
    return await _journeys().encender_runtime_v3(tenant_id)


def conexion_de(tenant_id: uuid.UUID) -> uuid.UUID | None:
    """La conexión que montó `montar_release` para el negocio (None si no se montó)."""
    return _journeys()._CONEXION_V3.get(tenant_id)


async def republicar(
    tenant_id: uuid.UUID,
    *,
    politicas_agente: dict[str, Any] | None = None,
    politicas_kernel: dict[str, Any] | None = None,
    agente: str = "atencion",
) -> dict[str, dict[str, Any]]:
    """Cambia las políticas de la versión del especialista (`agente`) y/o del Coordinador, publica
    versiones nuevas y promueve una release nueva en cada conexión con release activa. Configuración
    ya no edita agentes (ADR-0191 §4): un test que cambia un límite lo hace en la versión del agente.
    Devuelve las políticas anteriores (`{"agente": ..., "kernel": ...}`) para restaurarlas."""
    from sqlalchemy import text

    from core.agents import versiones
    from core.db.session import get_session
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager
    from core.releases.build import construir
    from core.releases.promocion import ConflictoDeRelease, promover
    from core.shared.tenant import tenant_scope

    modulos = plugin_manager.list_modules() or plugin_manager.scan()
    catalogo = catalogo_de_acciones(modulos)
    anteriores: dict[str, dict[str, Any]] = {}
    async with tenant_scope(tenant_id):
        for nombre, key, cambios in (("agente", agente, politicas_agente), ("kernel", "coordinador", politicas_kernel)):
            if not cambios:
                continue
            async with get_session() as session:
                aid, contenido = (
                    await session.execute(
                        text(
                            "select a.id, v.content from agents a join agent_versions v on v.agent_id = a.id "
                            "and v.tenant_id = a.tenant_id where a.tenant_id = :t and a.key = :k "
                            "and a.deleted_at is null order by v.version desc limit 1"
                        ),
                        {"t": tenant_id, "k": key},
                    )
                ).one()
            contenido = dict(contenido)
            anteriores[nombre] = dict(contenido.get("policies") or {})
            contenido["policies"] = {**anteriores[nombre], **cambios}
            await versiones.publicar(tenant_id, aid, catalogo=catalogo, contenido=contenido, notes="test")
        async with get_session() as session:
            conexiones = [
                fila[0]
                for fila in (
                    await session.execute(
                        text(
                            "select id from channel_connections where tenant_id = :t and deleted_at is null "
                            "and active_release_id is not null"
                        ),
                        {"t": tenant_id},
                    )
                ).all()
            ]
        for cid in conexiones:
            resultado = await construir(tenant_id, cid, actor="test", modules=modulos, fijadas={})
            try:
                await promover(tenant_id, cid, resultado.release_id, actor="test")
            except ConflictoDeRelease:  # el mismo contenido: la release ya es la activa
                pass
    return anteriores


async def release_inicial(tenant_id: uuid.UUID, modules: Any = None, **_: Any) -> Any:
    """La release 1 de las conexiones conversacionales del negocio, con agentes nativos (Coordinador +
    especialista «atencion», ADR-0192) en las que todavía no tienen quién atienda. Reemplaza en los
    tests a `backfill_releases`, que ya no crea el General desde el AgentSpec v2."""
    from sqlalchemy import text

    from core.db.session import get_session
    from core.plugin_manager import plugin_manager
    from core.releases.release_uno import backfill_releases
    from core.shared.tenant import tenant_scope

    modulos = list(modules) if modules is not None else (plugin_manager.list_modules() or plugin_manager.scan())
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sin_agente = [
                fila[0]
                for fila in (
                    await session.execute(
                        text(
                            "select c.id from channel_connections c join connection_types t on t.id = c.type_id "
                            "where c.tenant_id = :t and c.deleted_at is null and t.produces_conversations "
                            "and c.type_id <> 'playground' and not exists (select 1 from agent_instances i "
                            "where i.connection_id = c.id and i.role = 'specialist' and i.is_default "
                            "and i.deleted_at is null) order by c.created_at"
                        ),
                        {"t": tenant_id},
                    )
                ).all()
            ]
    for cid in sin_agente:
        await _journeys().agentes_nativos(tenant_id, cid, modulos)
    return await backfill_releases(dry_run=False, tenant_id=tenant_id, modules=modulos)
