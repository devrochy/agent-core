"""`agent-core tenant create` y `tenant purge` de punta a punta por el CLI (typer).

Regresión del 2026-10-25: el comando validaba con un `asyncio.run` y purgaba con otro;
el engine async (pool asyncpg) quedaba atado al primer loop y la purga fallaba siempre
con "attached to a different loop". Aquí el CLI corre en un hilo sin loop, con engine y
cliente Redis propios (recién creados), igual que en el contenedor.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _invoke_cli(args: list[str]):
    """Corre el CLI como en una terminal: proceso sin event loop y singletons nuevos.

    Los singletons del engine y de Redis de la sesión de tests viven en el loop de
    pytest; se apartan mientras corre el CLI (que crea los suyos y los cierra) y se
    reponen después.
    """
    import core.db.session as db_session
    import core.memory.working as working
    from cli.main import app

    guardados = (db_session._engine, db_session._session_factory, working._client)
    db_session._engine = db_session._session_factory = working._client = None
    try:
        return CliRunner().invoke(app, args)
    finally:
        db_session._engine, db_session._session_factory, working._client = guardados


async def _onboard_suspendido(prefix: str) -> tuple[uuid.UUID, str]:
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant

    slug = f"{prefix}-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(
        name=f"Tenant desechable {slug}",
        slug=slug,
        business_description="Purga por CLI (test genérico).",
        persona={"tone": "cercano"},
        packs=["pack_commerce"],
    )
    await deactivate_tenant(slug)
    return tid, slug


async def _existe(tid: uuid.UUID) -> bool:
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(select(Tenant.id).where(Tenant.id == tid))).first() is not None


async def _id_por_slug(slug: str) -> uuid.UUID | None:
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one_or_none()


async def test_cli_purge_borra_el_negocio_suspendido() -> None:
    tid, slug = await _onboard_suspendido("cli-purge")

    res = await asyncio.to_thread(_invoke_cli, ["tenant", "purge", slug, "--confirm", f"ELIMINAR {slug}"])

    assert res.exit_code == 0, res.output
    assert "eliminado por completo" in res.output
    assert not await _existe(tid)


async def test_cli_purge_frase_incorrecta_no_borra() -> None:
    tid, slug = await _onboard_suspendido("cli-purge-nofrase")

    res = await asyncio.to_thread(_invoke_cli, ["tenant", "purge", slug, "--confirm", "ELIMINAR otro"])

    assert res.exit_code == 1, res.output
    assert await _existe(tid)


async def test_cli_create_y_purge_por_cli() -> None:
    """`tenant create` hacía el alta y el seed de packs en `asyncio.run` distintos (mismo bug)."""
    from core.tenants import deactivate_tenant

    slug = f"cli-alta-{uuid.uuid4().hex[:6]}"
    res = await asyncio.to_thread(
        _invoke_cli,
        ["tenant", "create", "-y", "--name", f"Alta CLI {slug}", "--slug", slug, "--packs", "pack_commerce"],
    )
    assert res.exit_code == 0, res.output
    assert "Seed de packs completado" in res.output
    tid = await _id_por_slug(slug)
    assert tid is not None

    await deactivate_tenant(slug)
    res = await asyncio.to_thread(_invoke_cli, ["tenant", "purge", slug, "--confirm", f"ELIMINAR {slug}"])
    assert res.exit_code == 0, res.output
    assert not await _existe(tid)
