"""agent-core db: migraciones y seed."""

from __future__ import annotations

import subprocess
import sys

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def migrate() -> None:
    """Corre las migraciones del core + las de todos los plugins instalados."""
    from core.db.migrations import upgrade_all

    results = upgrade_all()
    console.print("✓ [green]Migraciones del core aplicadas[/green]")

    failed = False
    for plugin_id, status in results.items():
        if status.startswith("error:"):
            failed = True
            console.print(f"[red]✗ {plugin_id}: {status[7:][:150]}[/red]")
        elif status == "aplicadas":
            console.print(f"✓ [green]{plugin_id}: migraciones aplicadas[/green]")
        else:
            console.print(f"[dim]· {plugin_id}: sin migraciones propias[/dim]")

    if failed:
        console.print("\n[red]Alguna migración de plugin falló (ver arriba).[/red]")
        raise typer.Exit(1)


@app.command()
def seed() -> None:
    """Crea el usuario admin por defecto y la fila de configuración (si no existen)."""
    subprocess.run([sys.executable, "scripts/init_db.py"], check=True)
