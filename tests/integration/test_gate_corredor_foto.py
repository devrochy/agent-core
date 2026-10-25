"""HU-AOS-21 parte 3b · la foto de una release se monta idéntica en otra base (agentcore_eval).

Sin LLM ni red. Construye una release en la base del negocio, la exporta, la importa bajo un negocio
efímero en una SEGUNDA base y comprueba que `cargar_release` ahí da los mismos componentes y
procedimientos. Las dos bases son del stack efímero del test: la segunda se llama como
`agentcore_eval_test` (`_eval_db`).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import _eval_db
import pytest
from _api_helpers import uniq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def url_eval(rol: str = "runtime") -> str:
    return _eval_db.urls()[0 if rol == "runtime" else 1]


@pytest.fixture(scope="module", autouse=True)
async def _requisitos() -> None:
    from core.releases.packs import sync_packs

    await _eval_db.asegurar()
    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@asynccontextmanager
async def sesion_en_eval(tid: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Sesión del rol de runtime sobre la base de eval, con el `app.tenant_id` del efímero."""
    engine = create_async_engine(url_eval() or "")
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await session.execute(text("select set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
            yield session
            await session.commit()
    finally:
        await engine.dispose()


async def _negocio_con_release() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.releases.release_uno import backfill_releases

    slug = uniq("foto")
    tid, _ = await onboard_tenant(name=f"Foto {slug}", slug=slug, persona={"tone": "cercano"})
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid,
        t=tid,
        ext=uniq("ig"),
    )
    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    ((activa,),) = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    return tid, cid, activa


async def _efimero_en_eval(slug: str) -> uuid.UUID:
    """Fila `tenants` del negocio efímero, con el rol dueño de la base de eval."""
    tid = uuid.uuid4()
    engine = create_async_engine(url_eval("dueno") or "")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("insert into tenants (id, name, slug) values (:i, :n, :s)"), {"i": tid, "n": slug, "s": slug}
            )
    finally:
        await engine.dispose()
    return tid


async def test_la_foto_monta_la_misma_release_en_otra_base() -> None:
    from core.db.session import get_session
    from core.releases import snapshot
    from core.releases.cargar import cargar_release, vaciar_cache
    from core.shared.tenant import tenant_scope

    tid, cid, release_id = await _negocio_con_release()
    async with tenant_scope(tid):
        async with get_session() as session:
            foto = await snapshot.exportar(session, tid, release_id)
            origen = await cargar_release(session, tid, release_id)
    assert origen is not None
    assert foto["release"]["id"] == str(release_id)
    assert foto["componentes"] and foto["packs"] is not None
    # La foto no lleva datos del cliente: solo filas congeladas de la release.
    assert set(foto) == {"version", "release", "componentes", "versiones", "agentes", "instancias", "packs"}

    efimero = await _efimero_en_eval(uniq("foto-eval"))
    dueno = create_async_engine(url_eval("dueno") or "")
    try:
        async with dueno.begin() as conn:
            await snapshot.importar_pack_versions(conn, foto)
    finally:
        await dueno.dispose()
    vaciar_cache()
    async with tenant_scope(efimero):
        async with sesion_en_eval(efimero) as session:
            conexion = await snapshot.importar(session, efimero, foto)
        assert str(conexion) == str(cid)
        async with sesion_en_eval(efimero) as session:
            activa = (
                await session.execute(text("select active_release_id from channel_connections where id = :c"), {"c": conexion})
            ).scalar_one()
            assert activa == release_id
            copia = await cargar_release(session, efimero, release_id)
    assert copia is not None
    assert copia.manifest_hash == origen.manifest_hash
    assert sorted(c.key for c in copia.componentes) == sorted(c.key for c in origen.componentes)
    assert sorted(copia.procedimientos()) == sorted(origen.procedimientos())
    assert {c.version_id: c.content for c in copia.componentes} == {c.version_id: c.content for c in origen.componentes}


async def test_importar_exige_el_scope_del_negocio_efimero() -> None:
    from core.releases import snapshot

    with pytest.raises(PermissionError):
        await snapshot.importar(None, uuid.uuid4(), {"version": 1})


async def test_una_foto_de_otra_version_se_rechaza() -> None:
    from core.releases import snapshot
    from core.shared.tenant import tenant_scope

    tid = uuid.uuid4()
    async with tenant_scope(tid):
        with pytest.raises(snapshot.FotoInvalida):
            await snapshot.importar(None, tid, {"version": 99})
