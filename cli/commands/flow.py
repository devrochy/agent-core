"""agent-core flow: flujo de atención del tenant (ADR-0054, ADR-0109).

El flujo efectivo se compone: `core/flow.json` + `packages/pack-*/flow.json` de los
packs habilitados + las secciones propias del negocio en `TENANTS_ROOT/<slug>/flow.json`
(que ya no es autoritativo). Estos comandos lo muestran, validan, normalizan y migran.

Uso:
  agent-core flow show <slug>
  agent-core flow validate <slug>
  agent-core flow sync <slug>
  agent-core flow migrate <slug> [--apply]
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from core.shared.logger import get_logger

app = typer.Typer(no_args_is_help=True)
console = Console()
log = get_logger(__name__)


async def _load_spec_and_slug(slug: str):  # type: ignore[no-untyped-def]
    """Devuelve (spec, slug) del tenant o lanza typer.BadParameter si no existe."""
    from core.agents.spec import load_active_spec
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with get_session() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
    if tenant is None:
        raise typer.BadParameter(f"Tenant no existe: {slug}")
    async with tenant_scope(tenant.id):
        spec = await load_active_spec(tenant.id)
    return spec, slug


def _available_tool_names(spec) -> set[str]:  # type: ignore[no-untyped-def]
    """Tools disponibles PARA ESTE TENANT: packs habilitados + core + declarativas.

    Mismo criterio que el runtime (`loop.py`): solo los packs de `spec.packs`; así
    `flow validate` no da ✓ a una tool que en producción `_tool_denial` denegaría.
    """
    from core.agents.spec import enabled_packs
    from core.plugin_manager import plugin_manager

    enabled = enabled_packs(spec)
    names: set[str] = set()
    for module in plugin_manager.scan():
        if module.id not in enabled:
            continue
        for skill_cls in getattr(module, "declared_skills", []) or []:
            try:
                for tool in skill_cls().get_tools():
                    names.add(tool.name)
            except Exception:
                log.warning("flow_cli_skill_read_failed", plugin=module.id, exc_info=True)
    try:
        from core.tools.core_skill import CoreSkill

        for tool in CoreSkill().get_tools():
            names.add(tool.name)
    except Exception:
        log.warning("flow_cli_core_skill_read_failed", exc_info=True)
    from core.mcp.declarative import tools_for_spec

    for tool in tools_for_spec(spec):
        names.add(tool.name)
    return names


@app.command()
def show(slug: str = typer.Argument(..., help="Slug del tenant")) -> None:
    """Muestra el flujo de atención resuelto del tenant."""
    from core.agents.flow import resolve_flow

    async def _run() -> None:
        spec, _ = await _load_spec_and_slug(slug)
        flow = await resolve_flow(spec, slug=slug)
        if flow is None:
            console.print(
                f"[yellow]Sin flujo para {slug}[/yellow] "
                "(no hay flow.json de tenant ni de sus packs habilitados)"
            )
            return
        table = Table(title=f"Flujo de atención · {slug} (fuente: {flow.source})")
        table.add_column("#", justify="right")
        table.add_column("Sección")
        table.add_column("Herramientas")
        for index, section in enumerate(flow.sections, start=1):
            table.add_row(str(index), section.label, ", ".join(section.tools) or "—")
        console.print(table)

    asyncio.run(_run())


@app.command()
def validate(slug: str = typer.Argument(..., help="Slug del tenant")) -> None:
    """Valida que las tools del flujo existan y estén disponibles para el tenant."""
    from core.agents.flow import resolve_flow

    async def _run() -> None:
        spec, _ = await _load_spec_and_slug(slug)
        available = _available_tool_names(spec)
        flow = await resolve_flow(spec, slug=slug, available=available)
        if flow is None:
            console.print(f"[yellow]Sin flujo para {slug}[/yellow]")
            return
        if flow.unknown_tools:
            console.print(
                f"[red]✗ {len(flow.unknown_tools)} tool(s) no disponibles:[/red] "
                + ", ".join(flow.unknown_tools)
            )
            raise typer.Exit(1)
        console.print(
            f"✓ [green]Flujo válido[/green] ({len(flow.sections)} secciones, "
            f"{len(flow.tool_names)} tools, fuente: {flow.source})"
        )

    asyncio.run(_run())


@app.command()
def sync(
    slug: str = typer.Argument(..., help="Slug del tenant"),
    packs: str = typer.Option("", "--packs", help="Coma-separado (default: packs del spec)"),
) -> None:
    """Normaliza TENANTS_ROOT/<slug>/flow.json: quita copias de packs (ADR-0109)."""
    from core.agents.flow import pack_flow_path, seed_tenant_flow

    async def _run() -> None:
        spec, _ = await _load_spec_and_slug(slug)
        pack_ids = [p.strip() for p in packs.split(",") if p.strip()] or sorted(spec.packs)
        if not pack_ids:
            console.print(f"[yellow]{slug} no tiene packs habilitados[/yellow]")
            return
        # Descartar packs sin flow.json (ej. un typo en --packs): sin esto, un id
        # inexistente borraría las secciones del tenant con ese `pack` sin reponer nada.
        missing = [p for p in pack_ids if pack_flow_path(p) is None]
        if missing:
            console.print(f"[yellow]Sin flow.json: {', '.join(missing)}[/yellow]")
            pack_ids = [p for p in pack_ids if p not in missing]
        if not pack_ids:
            return
        steps = seed_tenant_flow(slug, pack_ids)
        if not steps:
            console.print(f"[yellow]Ninguno de {pack_ids} declara flujo[/yellow]")
            return
        for step in steps:
            console.print(f"  · {step}")

    asyncio.run(_run())


@app.command()
def migrate(
    slug: str = typer.Argument(..., help="Slug del tenant"),
    apply: bool = typer.Option(False, "--apply", help="Escribe el archivo (por defecto solo muestra el diff)"),
) -> None:
    """Migra el flow.json del negocio al modelo compuesto (HU-FWK-01, ADR-0109).

    Quita las copias de secciones de packs, convierte en propias (`tenant`) las que
    su pack no declara y avisa de overrides y de tools que ningún flow.json declara.
    """
    from core.agents.flow import normalize_tenant_flow

    async def _run() -> None:
        await _load_spec_and_slug(slug)
        migracion = normalize_tenant_flow(slug, apply=apply)
        titulo = "aplicada" if apply else "dry-run (usa --apply para escribir)"
        console.print(f"[bold]Migración del flujo · {slug} · {titulo}[/bold]")
        if not migracion.cambia:
            console.print("  · sin cambios")
        for linea in migracion.resumen():
            console.print(f"  · {linea}")

    asyncio.run(_run())
