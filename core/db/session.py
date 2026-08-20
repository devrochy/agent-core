"""Sesiones SQLAlchemy 2.0 async.

Patrón único de acceso a Postgres en el core y en los plugins:
    async with get_session() as session: ...

Para tareas Celery (contexto síncrono) se usa `run_async()` que envuelve
la corrutina en asyncio.run() — un solo engine y un solo estilo de código.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Coroutine

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():  # type: ignore[no-untyped-def]
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Puente para tareas Celery: ejecuta una corrutina en contexto síncrono."""
    return asyncio.run(coro)
