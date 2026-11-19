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
    fijadas: dict[uuid.UUID, uuid.UUID] = {}
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
            # Restaurar repite una versión anterior y `publicar` la reutiliza: se fija esa al build.
            version, _ = await versiones.publicar(tenant_id, aid, catalogo=catalogo, contenido=contenido, notes="test")
            fijadas[aid] = version.id
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
            resultado = await construir(tenant_id, cid, actor="test", modules=modulos, fijadas=fijadas)
            try:
                await promover(tenant_id, cid, resultado.release_id, actor="test")
            except ConflictoDeRelease:  # el mismo contenido: la release ya es la activa
                pass
    return anteriores


async def _catalogos_del_arranque(modulos: list[Any]) -> None:
    """Lo que espeja el arranque de la app y los tests no corren: el catálogo de tipos de conexión
    (p. ej. `whatsapp_waweb`) y `pack_versions` (sin él, el build falla `habilidad_no_disponible`).
    Con xdist cada worker puede llegar aquí sin que otro archivo lo haya hecho antes."""
    from core.gateway.connection_types import sync_connection_types
    from core.releases.packs import sync_packs

    await sync_connection_types()
    reporte = await sync_packs(modulos)
    assert not reporte.fallo, reporte.errores


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
    await _catalogos_del_arranque(modulos)
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


async def conceder_herramientas(
    tenant_id: uuid.UUID, bindings: list[dict[str, Any]], *, extra: list[Any] = (), agente: str = "atencion",
    quitar: bool = False,
) -> None:
    """La versión del especialista con estos bindings (reemplazan a los de la misma tool; `quitar` deja la
    versión sin ninguna de esas tools) y una release nueva en cada conexión activa. `extra`: `MCPTool` de
    prueba que no están en el binario; entran al catálogo solo mientras se publica y se construye."""
    import types

    import pytest

    from core.kernel.procedures import catalogo as _catalogo

    parche = pytest.MonkeyPatch()
    if extra:
        originales = _catalogo._core_skills
        skill = types.SimpleNamespace(get_tools=lambda: list(extra))
        parche.setattr(_catalogo, "_core_skills", lambda: [*originales(), skill])
        parche.setattr(_catalogo, "_cache", None)
    try:
        await _republicar_tools(tenant_id, bindings, agente=agente, quitar=quitar)
    finally:
        parche.undo()
        if extra:
            _catalogo._cache = None


async def _republicar_tools(tenant_id: uuid.UUID, bindings: list[dict[str, Any]], *, agente: str, quitar: bool) -> None:
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
    nombres = {b["tool"] for b in bindings}
    consulta = text(
        "select a.id, v.content from agents a join agent_versions v on v.agent_id = a.id "
        "and v.tenant_id = a.tenant_id where a.tenant_id = :t and a.key = :k "
        "and a.deleted_at is null order by v.version desc limit 1"
    )
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            fila = (await session.execute(consulta, {"t": tenant_id, "k": agente})).one_or_none()
    if fila is None:  # negocio sin conexión todavía: se le monta una con su especialista
        await montar_release_en(tenant_id)
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                fila = (await session.execute(consulta, {"t": tenant_id, "k": agente})).one()
    aid, contenido = fila
    async with tenant_scope(tenant_id):
        contenido = dict(contenido)
        tools = [dict(b) for b in contenido.get("tools") or [] if b.get("tool") not in nombres]
        contenido["tools"] = tools if quitar else [*tools, *(dict(b) for b in bindings)]
        # Un contenido igual a una versión anterior la reutiliza: se fija esa al build, no la última.
        version, _ = await versiones.publicar(tenant_id, aid, catalogo=catalogo, contenido=contenido, notes="test")
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
            resultado = await construir(tenant_id, cid, actor="test", modules=modulos, fijadas={aid: version.id})
            try:
                await promover(tenant_id, cid, resultado.release_id, actor="test")
            except ConflictoDeRelease:
                pass


# canal de la prueba → (canal de la conexión, proveedor, tipo)
_TIPO_POR_CANAL = {
    "whatsapp": ("whatsapp", "meta", "whatsapp_meta"),
    "qr": ("whatsapp", "waweb", "whatsapp_waweb"),
    "instagram": ("instagram", "meta", "instagram_meta"),
    "messenger": ("messenger", "meta", "messenger_meta"),
    "web": ("web", "web", "webchat"),
}


async def montar_release_en(tenant_id: uuid.UUID, canal: str = "whatsapp") -> uuid.UUID:
    """Una conexión del `canal` con Coordinador + especialista nativos y su release 1 (ADR-0191/0192).
    Las conversaciones del mismo canal que el test cree sin conexión la toman (conftest); `qr` es
    WhatsApp por QR (waweb)."""
    from sqlalchemy import text

    from core.db.session import get_session
    from core.plugin_manager import plugin_manager
    from core.releases.release_uno import backfill_releases
    from core.shared.tenant import tenant_scope

    canal_conexion, proveedor, tipo = _TIPO_POR_CANAL[canal]
    await _catalogos_del_arranque(plugin_manager.list_modules() or plugin_manager.scan())
    conexion = uuid.uuid4()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await session.execute(
                text(
                    "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
                    "values (:id, :t, :canal, :prov, :ext, :tipo)"
                ),
                {"id": conexion, "t": tenant_id, "canal": canal_conexion, "prov": proveedor,
                 "ext": f"pruebas-{conexion.hex[:12]}", "tipo": tipo},
            )
    modulos = plugin_manager.list_modules() or plugin_manager.scan()
    await _journeys().agentes_nativos(tenant_id, conexion, modulos)
    reporte = await backfill_releases(dry_run=False, tenant_id=tenant_id, modules=modulos)
    assert not reporte.fallo, reporte.errores
    return conexion
