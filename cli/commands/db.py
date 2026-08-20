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
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    console.print("✓ Migraciones del core aplicadas")

    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    for module in plugin_manager.list_modules():
        try:
            plugin_manager._run_plugin_migrations(module.id)
        except Exception as exc:
            console.print(f"[yellow]⚠ {module.id}: {str(exc)[:100]}[/yellow]")
    console.print("✓ Migraciones de plugins aplicadas")


@app.command()
def seed() -> None:
    """Crea el usuario admin por defecto y la fila de configuración (si no existen)."""
    subprocess.run([sys.executable, "scripts/init_db.py"], check=True)
