"""agent-core sdd: gestión del System Design Document vivo."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.panel import Panel

from cli.sdd_generator import load_state, render_all

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def update() -> None:
    """Regenera los documentos sdd/*.md desde el estado actual."""
    state = load_state()
    if not state:
        console.print("[red]No hay estado SDD. Corre primero: agent-core init[/red]")
        raise typer.Exit(1)
    files = render_all(state)
    for file in files:
        console.print(f"✓ [green]{file}[/green]")


@app.command()
def show() -> None:
    """Muestra el estado del SDD en consola."""
    state = load_state()
    if not state:
        console.print("[red]No hay estado SDD. Corre primero: agent-core init[/red]")
        raise typer.Exit(1)
    console.print(Panel(json.dumps(state, indent=2, ensure_ascii=False), title="sdd/.state.json"))
