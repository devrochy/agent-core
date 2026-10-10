"""agent-core procedures: validador y linter de `procedure/1` (ADR-0113, HU-AOS-03).

Uso:
  agent-core procedures lint <archivo.json> [<archivo.json> ...]

Valida la forma del contrato y aplica el linter contra las acciones del binario (core
y todas las habilidades instaladas): acciones inexistentes, etapas inalcanzables,
acciones irreversibles sin confirmación y afirmaciones de dinero sin fuente, con un
mensaje por problema. Sale con código 1 si algún archivo tiene problemas. No toca la
base de datos.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

app = typer.Typer(no_args_is_help=True)
console = Console()


def _leer(path: Path) -> tuple[object | None, str | None]:
    from core.kernel.procedures import MAX_PROCEDURE_BYTES

    try:
        # Se lee a lo sumo un byte más del tope: un archivo enorme no se carga entero.
        with path.open("rb") as archivo:
            raw = archivo.read(MAX_PROCEDURE_BYTES + 1)
    except OSError as exc:
        return None, f"no se pudo leer ({exc.strerror})"
    if len(raw) > MAX_PROCEDURE_BYTES:
        return None, f"pesa más de {MAX_PROCEDURE_BYTES // 1024} KiB"
    try:
        return json.loads(raw), None
    except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError y enteros enormes
        return None, f"JSON inválido ({str(exc)[:120]})"
    except RecursionError:
        return None, "JSON inválido (anidado en exceso)"


@app.command()
def lint(
    archivos: Annotated[list[Path], typer.Argument(help="Procedimientos procedure/1 en JSON")],
) -> None:
    """Valida y aplica el linter a uno o más procedimientos."""
    from core.kernel.procedures import catalogo_de_acciones, revisar
    from core.plugin_manager import plugin_manager

    catalogo = catalogo_de_acciones(plugin_manager.scan())
    con_problemas = 0
    for path in archivos:
        raw, error = _leer(path)
        if error is not None:
            con_problemas += 1
            console.print(f"[red]✗[/red] {escape(str(path))}: {escape(error)}")
            continue
        issues = revisar(raw, catalogo)
        if not issues:
            console.print(f"[green]✓[/green] {escape(str(path))}")
            continue
        con_problemas += 1
        console.print(f"[red]✗[/red] {escape(str(path))}: {len(issues)} problema(s)")
        for issue in issues:
            console.print(f"    {escape(issue.path)}: {escape(issue.message)} [dim]({issue.code})[/dim]")
    if con_problemas:
        console.print(f"[red]{con_problemas} de {len(archivos)} procedimiento(s) con problemas.[/red]")
        raise typer.Exit(code=1)
