"""Sesiones SQLAlchemy 2.0 async.

Patrón único de acceso a Postgres en el core y en los plugins:
    async with get_session() as session: ...

Para tareas Celery (contexto síncrono) se usa `run_async()` que envuelve
la corrutina en asyncio.run() — un solo engine y un solo estilo de código.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings
from core.shared.tenant import get_current_tenant_id

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
            tenant_id = get_current_tenant_id()
            if tenant_id is not None:
                # Scope de TRANSACCIÓN (is_local=true): muere con el COMMIT, no se
                # filtra entre conexiones del pool. ADR-0012.
                await session.execute(
                    text("select set_config('app.tenant_id', :t, true)"),
                    {"t": str(tenant_id)},
                )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """Puente para tareas Celery: ejecuta la corrutina en el loop persistente
    del proceso (ver `core/shared/async_bridge.py`) -- NO `asyncio.run`, que
    recrearía el loop por task y rompería el engine/cliente Redis cacheados."""
    from core.shared.async_bridge import run_async as _run_async

    return _run_async(coro)


async def get_owned(
    session: AsyncSession, model: type[Any], id_: Any, tenant_id: uuid.UUID | None = None
) -> Any | None:
    """Como `session.get(model, id_)` pero con un filtro EXPLÍCITO de
    `tenant_id` -- defensa en profundidad más allá de RLS (ADR-0012: RLS
    sigue siendo la garantía real; esto es un segundo cinturón que no
    depende de que el contexto de sesión de Postgres esté bien seteado).

    Requiere que `model` tenga una columna `tenant_id`. Devuelve `None` si
    no existe o pertenece a otro tenant (mismo comportamiento "no
    encontrado" que `session.get()` -- no se distinguen los dos casos,
    tampoco lo hacía RLS).
    """
    tid = tenant_id if tenant_id is not None else get_current_tenant_id()
    return (
        await session.execute(select(model).where(model.id == id_, model.tenant_id == tid))
    ).scalar_one_or_none()
