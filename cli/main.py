"""CLI de agent-core.

Comandos:
  agent-core init                         Bootstrap de una instalación nueva (.env + sdd/ + BD + admin)
  agent-core tenant create|list|seed|docs|deactivate|activate|purge  Alta y operación de tenants (multi-tenant)
  agent-core admin create|link|grant|revoke|password|list   Admins del panel y sus permisos (ADR-0047)
  agent-core status                       Estado del sistema
  agent-core configure [tone|llm]         Reconfiguración puntual
  agent-core plugin list|install|remove|scaffold
  agent-core db migrate|seed
  agent-core sdd update|show
  agent-core meta doctor                  Diagnóstico de WhatsApp Cloud API (Meta)
"""

from __future__ import annotations

import typer

from cli.commands.admin import app as admin_app
from cli.commands.agent import app as agent_app
from cli.commands.configure import app as configure_app
from cli.commands.db import app as db_app
from cli.commands.doctor import doctor_command
from cli.commands.init import init_command
from cli.commands.meta import app as meta_app
from cli.commands.plugin import app as plugin_app
from cli.commands.sdd import app as sdd_app
from cli.commands.tenant import app as tenant_app

app = typer.Typer(
    name="agent-core",
    help="Framework multi-tenant de agentes IA: un solo código base sirve a todos los clientes.",
    no_args_is_help=True,
)

app.command(name="init")(init_command)
app.command(name="doctor")(doctor_command)
app.add_typer(plugin_app, name="plugin", help="Gestión de plugins")
app.add_typer(configure_app, name="configure", help="Reconfiguración del asistente")
app.add_typer(db_app, name="db", help="Base de datos")
app.add_typer(sdd_app, name="sdd", help="System Design Document vivo")
app.add_typer(agent_app, name="agent", help="Releases y rollback del AgentSpec (ADR-0009/0016)")
app.add_typer(tenant_app, name="tenant", help="Alta y operación de tenants (multi-tenant, ADR-0001)")
app.add_typer(admin_app, name="admin", help="Admins del panel: alta y permisos (ADR-0047)")
app.add_typer(meta_app, name="meta", help="Diagnóstico de WhatsApp Cloud API (Meta)")


@app.command()
def status() -> None:
    """Estado del sistema: BD, Redis, plugins."""
    import asyncio

    from rich.console import Console
    from rich.table import Table

    console = Console()

    async def _check() -> None:
        table = Table(title="agent-core · estado")
        table.add_column("Componente")
        table.add_column("Estado")

        # BD
        try:
            from sqlalchemy import text

            from core.db.session import get_session

            async with get_session() as session:
                await session.execute(text("SELECT 1"))
            table.add_row("PostgreSQL", "[green]OK[/green]")
        except Exception as exc:
            table.add_row("PostgreSQL", f"[red]ERROR: {str(exc)[:60]}[/red]")

        # Redis
        try:
            from core.memory.working import get_redis

            await get_redis().ping()
            table.add_row("Redis", "[green]OK[/green]")
        except Exception as exc:
            table.add_row("Redis", f"[red]ERROR: {str(exc)[:60]}[/red]")

        # Plugins
        from core.plugin_manager import plugin_manager

        modules = plugin_manager.scan()
        table.add_row("Plugins detectados", str(len(modules)))
        for module in modules:
            table.add_row(f"  · {module.id}", module.version)

        console.print(table)

    asyncio.run(_check())


if __name__ == "__main__":
    app()
