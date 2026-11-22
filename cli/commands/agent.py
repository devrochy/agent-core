"""agent-core agent: linter de `agentspec/3` y `kernelspec/1` (ADR-0113).

Las versiones y el rollback del AgentSpec v2 se retiraron (ADR-0206, HU-AOS-91): las versiones son de cada agente
y las releases se revierten desde Conexiones.

`agent-core agent lint <archivo.json> ...` valida `agentspec/3` y `kernelspec/1` con el mismo
validador y linter de CI y de la API (ADR-0113, HU-AOS-12). No toca la base de datos.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.callback()
def _grupo() -> None:
    """Linter de agentspec/3 y kernelspec/1 (ADR-0113)."""
    # Con un solo comando, Typer lo volvería el comando por defecto: el callback mantiene `agent lint`.


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
