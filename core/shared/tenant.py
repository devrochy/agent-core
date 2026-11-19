"""Contexto de tenant por request/tarea (ADR-0012).

`tenant_scope` establece el tenant actual en un contextvar. `get_session()` lo lee y
ejecuta `set_config('app.tenant_id', <id>, true)` — scope de TRANSACCIÓN: el contexto
muere con el COMMIT y no se filtra entre conexiones del pool.

Todo punto de entrada pasa por `tenant_scope`: middleware de FastAPI (panel y webhooks),
consumer, debounce fire, recovery, jobs Celery y CLI/bootstrap.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

_current_tenant_id: ContextVar[str | None] = ContextVar("current_tenant_id", default=None)


def get_current_tenant_id() -> uuid.UUID | None:
    raw = _current_tenant_id.get()
    if raw is None:
        return None
    return uuid.UUID(str(raw))


def set_current_tenant_id(tenant_id: uuid.UUID | None) -> None:
    """Fija el tenant sin contexto (uso interno); prefiere `tenant_scope`."""
    _current_tenant_id.set(str(tenant_id) if tenant_id is not None else None)


@asynccontextmanager
async def tenant_scope(tenant_id: uuid.UUID | str | None) -> AsyncIterator[None]:
    token = _current_tenant_id.set(str(tenant_id) if tenant_id is not None else None)
    try:
        yield
    finally:
        _current_tenant_id.reset(token)


async def resolve_tenant_for_phone(phone_number_id: str) -> uuid.UUID | None:
    """Resuelve el tenant de un webhook por phone_number_id (SECURITY DEFINER).

    Función exenta de RLS: es el bootstrap chicken-and-egg (ADR-0012). Devuelve None
    si el número no pertenece a ninguna conexión.
    """
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(
                text("select resolve_tenant_for_phone(:p)"), {"p": phone_number_id}
            )
        ).scalar_one_or_none()


async def admin_tenants(admin_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
    """Tenants a los que un admin tiene acceso, con nombre (SECURITY DEFINER, sin RLS).

    Devuelve (tenant_id, tenant_name, role). Usado por el middleware y el selector
    de tenant del panel (C3, Sprint 4).
    """
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        rows = (
            await session.execute(
                text("select tenant_id, tenant_name, role from admin_tenants(:aid)"),
                {"aid": str(admin_id)},
            )
        ).all()
        return [(uuid.UUID(str(r[0])), str(r[1]), str(r[2])) for r in rows]


@dataclass(slots=True)
class NegociosDeCuenta:
    """Negocios de una cuenta separados por estado (ADR-0136).

    `operativos`: los que puede usar en esta sesión (tuplas `(tenant_id, nombre, rol)` como
    `admin_tenants`). `suspendidos`: ids vinculados por `tenant_user` que están suspendidos o
    borrados; una cuenta que solo tiene de esos no entra (403 `tenant_suspended`). Para quien
    actúa como master (master, tester habilitado) no se filtra nada: el master entra a un
    negocio suspendido para reactivarlo o purgarlo.
    """

    operativos: list[tuple[uuid.UUID, str, str]]
    suspendidos: set[uuid.UUID]

    @property
    def solo_suspendidos(self) -> bool:
        return not self.operativos and bool(self.suspendidos)


async def negocios_de_cuenta(admin: Any) -> NegociosDeCuenta:
    """`admin_tenants` filtrado por el ciclo de vida del negocio (ADR-0136).

    Único punto para resolver qué negocios ve una sesión: login, `get_principal`, el
    middleware y el selector del panel clásico, la lista de `/admin/tenants` y la app de
    campo. Las ramas `global` de `admin_tenants()` ya vienen filtradas por `status`; aquí se
    filtran los vínculos directos de `tenant_user`, que la función SQL devuelve tal cual.
    """
    from sqlalchemy import select

    from core.admin.roles import actua_como_master
    from core.db.models import Tenant
    from core.db.session import get_session

    tenants = await admin_tenants(admin.id)
    if not tenants or actua_como_master(admin):
        return NegociosDeCuenta(operativos=list(tenants), suspendidos=set())
    async with get_session() as session:
        activos = set(
            (
                await session.execute(
                    select(Tenant.id).where(
                        Tenant.id.in_([t[0] for t in tenants]),
                        Tenant.status == "active",
                        Tenant.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    return NegociosDeCuenta(
        operativos=[t for t in tenants if t[0] in activos],
        suspendidos={t[0] for t in tenants if t[0] not in activos},
    )


async def default_tenant() -> uuid.UUID | None:
    """Primer tenant activo (bootstrap / dev). None si no hay ninguno."""
    from sqlalchemy import text

    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(text("select default_tenant_id()"))
        ).scalar_one_or_none()


async def tenant_slug(tenant_id: uuid.UUID) -> str | None:
    """Slug del tenant (tabla de sistema, sin RLS). `None` si no existe.

    Usado para resolver `TENANTS_ROOT/<slug>/` (ADR-0026, playbooks por
    tenant) sin necesitar una función SQL nueva -- `Tenant` ya se consulta
    sin RLS por diseño (ver docstring del modelo).
    """
    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        return (
            await session.execute(select(Tenant.slug).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()


async def current_tenant_slug() -> str | None:
    """Slug del negocio del `tenant_scope` actual. `None` fuera de un scope.

    Fuente para resolver `TENANTS_ROOT/<slug>/` (playbooks, flow.json): el negocio
    sale SIEMPRE de la sesión, nunca del `tenantId` del AgentSpec, que viaja en el
    JSON que guarda el propio negocio y podría apuntar a otro.
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return None
    return await tenant_slug(tenant_id)
