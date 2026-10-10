"""agent-core agent: release y rollback de AgentSpec (ADR-0009/0016) y linter de v3.

El rollback es un flip de `is_active` en `agent_specs` (un paso). El canary se
resuelve por el gate de evals + tenant staging (ADR-0016).

`agent-core agent lint <archivo.json> ...` valida `agentspec/3` y `kernelspec/1` con el mismo
validador y linter de CI y de la API (ADR-0113, HU-AOS-12). No toca la base de datos.
"""

from __future__ import annotations

import asyncio
import builtins
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
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

def _leer_spec(path: Path) -> tuple[object | None, str | None]:
    from core.agents.contrato_v3 import MAX_SPEC_BYTES

    try:
        # A lo sumo un byte más del tope: un archivo enorme no se carga entero.
        with path.open("rb") as archivo:
            raw = archivo.read(MAX_SPEC_BYTES + 1)
    except OSError as exc:
        return None, f"no se pudo leer ({exc.strerror})"
    if len(raw) > MAX_SPEC_BYTES:
        return None, f"pesa más de {MAX_SPEC_BYTES // 1024} KiB"
    try:
        return json.loads(raw), None
    except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError y enteros enormes
        return None, f"JSON inválido ({str(exc)[:120]})"
    except RecursionError:
        return None, "JSON inválido (anidado en exceso)"


@app.command()
def lint(
    archivos: Annotated[builtins.list[Path], typer.Argument(help="AgentSpec v3 o KernelSpec v1 en JSON")],
) -> None:
    """Valida y aplica el linter a uno o más `agentspec/3` o `kernelspec/1` (ADR-0113)."""
    from core.agents.contrato_v3 import revisar_spec
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    catalogo = catalogo_de_acciones(plugin_manager.scan())
    con_problemas = 0
    for path in archivos:
        raw, error = _leer_spec(path)
        if error is not None:
            con_problemas += 1
            console.print(f"[red]✗[/red] {escape(str(path))}: {escape(error)}")
            continue
        issues = revisar_spec(raw, catalogo)
        if not issues:
            console.print(f"[green]✓[/green] {escape(str(path))}")
            continue
        con_problemas += 1
        console.print(f"[red]✗[/red] {escape(str(path))}: {len(issues)} problema(s)")
        for issue in issues:
            console.print(f"    {escape(issue.path)}: {escape(issue.message)} [dim]({issue.code})[/dim]")
    if con_problemas:
        console.print(f"[red]{con_problemas} de {len(archivos)} spec(s) con problemas.[/red]")
        raise typer.Exit(code=1)
