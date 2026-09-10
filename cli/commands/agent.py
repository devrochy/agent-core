"""agent-core agent: release y rollback de AgentSpec (ADR-0009/0016).

El rollback es un flip de `is_active` en `agent_specs` (un paso). El canary se
resuelve por el gate de evals + tenant staging (ADR-0016).
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from cli.tenant_ref import resolve_tenant as _resolve_tenant

app = typer.Typer(no_args_is_help=True)
console = Console()


def _playbooks_count(spec: dict) -> int:
    return len((spec or {}).get("playbooks", []) or [])


@app.command()
def list(ref: str = typer.Argument("tenant-1", help="Tenant (slug o UUID)")) -> None:
    """Lista las versiones del AgentSpec del tenant."""

    async def _run() -> None:
        from core.db.models import AgentSpecRecord
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        tid = await _resolve_tenant(ref)
        async with tenant_scope(tid):
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(AgentSpecRecord)
                        .where(AgentSpecRecord.tenant_id == tid)
                        .order_by(AgentSpecRecord.created_at)
                    )
                ).scalars().all()
        table = Table(title=f"AgentSpec · {ref}")
        table.add_column("Versión")
        table.add_column("Activa")
        table.add_column("Playbooks")
        table.add_column("Creada")
        for r in rows:
            table.add_row(
                r.version,
                "SÍ" if r.is_active else "no",
                str(_playbooks_count(r.spec)),
                r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "—",
            )
        console.print(table)

    asyncio.run(_run())


@app.command()
def activate(
    version: str = typer.Argument(..., help="Versión a activar (publicar)"),
    ref: str = typer.Argument("tenant-1", help="Tenant (slug o UUID)"),
) -> None:
    """Activa una versión del AgentSpec (publicar). Rollback = activar otra versión."""

    async def _run() -> None:
        from core.db.models import AgentSpecRecord
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        tid = await _resolve_tenant(ref)
        async with tenant_scope(tid):
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(AgentSpecRecord).where(AgentSpecRecord.tenant_id == tid)
                    )
                ).scalars().all()
                target = next((r for r in rows if r.version == version), None)
                if target is None:
                    raise typer.BadParameter(f"Versión no existe: {version}")
                # Dos fases: primero ninguna activa, luego la target (el índice único
                # permite una sola activa por tenant).
                for r in rows:
                    r.is_active = False
                await session.flush()
                target.is_active = True
        console.print(f"✓ Versión {version} activada para {ref}")

    asyncio.run(_run())


@app.command()
def rollback(
    ref: str = typer.Argument("tenant-1", help="Tenant (slug o UUID)"),
    version: str | None = typer.Option(None, "--version", "-v", help="Versión exacta a revertir"),
) -> None:
    """Revierte a la versión anterior (o a --version). Rollback en un paso."""

    async def _run() -> None:
        from core.db.models import AgentSpecRecord
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        tid = await _resolve_tenant(ref)
        async with tenant_scope(tid):
            async with get_session() as session:
                rows = sorted(
                    (
                        await session.execute(
                            select(AgentSpecRecord).where(AgentSpecRecord.tenant_id == tid)
                        )
                    ).scalars().all(),
                    key=lambda r: r.created_at,
                )
                if not rows:
                    raise typer.BadParameter(f"Sin AgentSpec para {ref}")
                current = next((r for r in rows if r.is_active), rows[-1])
                target_version = version
                if target_version is None:
                    previous = next(
                        (r for r in reversed(rows) if r.created_at < current.created_at), None
                    )
                    if previous is None:
                        raise typer.BadParameter("No hay versión anterior para revertir")
                    target_version = previous.version
                target = next((r for r in rows if r.version == target_version), None)
                if target is None:
                    raise typer.BadParameter(f"Versión no existe: {target_version}")
                # Dos fases (índice único: una activa por tenant)
                for r in rows:
                    r.is_active = False
                await session.flush()
                target.is_active = True
        console.print(f"✓ {ref} revirtió de {current.version} a {target_version}")

    asyncio.run(_run())